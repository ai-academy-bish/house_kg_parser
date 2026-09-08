"""The crawl pipeline: sweep → observations → new listings → entities → report.

A run is a **snapshot**: one dated measurement of the whole board. The ordering is
what makes a repeat run cheap.

    1. sweep every result page          ~2.6k requests, and it yields a full
                                        observation per live listing — price,
                                        views, favourites, bump, promotion
    2. diff against the previous snapshot
    3. detail-crawl only what is new    ~10% of the board in a typical week
    4. re-read companies and complexes  ~900 profiles; ratings and reviews move
    5. close the snapshot and report

Stage 1 is also the only source of *disappearance*: nothing on house.kg announces
that a listing was sold or withdrawn, so a listing that no longer appears in any
stream is what "delisted" means here. That inference is only safe when the sweep
was complete, which is why `refresh.min_completeness` exists and why every run
writes a `snapshots` row saying how much of the board it actually saw.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from ..config import Config
from ..constants import CHANGE_BUMPED, CHANGE_FIELD
from ..http_client import HttpClient
from ..logging_utils import ProgressTracker, get_logger
from ..models import CardObservation, Observation, Snapshot
from ..storage import Storage
from .differ import Differ
from .entity_crawler import EntityCrawler
from .listing_crawler import ListingCrawler
from .url_collector import ListingRef, Sweep, UrlCollector

logger = get_logger(__name__)


@dataclass(slots=True)
class RunReport:
    """What one snapshot changed — the summary printed at the end of a run."""

    snapshot_id: str
    mode: str
    previous_snapshot_id: str | None = None

    pages_scanned: int = 0
    pages_failed: int = 0
    listings_live: int = 0
    listings_new: int = 0
    listings_delisted: int = 0
    listings_reappeared: int = 0
    listings_content_changed: int = 0
    photos_new: int = 0

    #: field name -> how many listings moved it (price_usd, promo, ...)
    field_changes: dict[str, int] = field(default_factory=dict)
    listings_bumped: int = 0

    companies_new: int = 0
    companies_changed: int = 0
    complexes_new: int = 0
    complexes_changed: int = 0
    reviews_new: int = 0
    users_new: int = 0

    complete: bool = True
    delisting_skipped: str = ""
    duration_seconds: float = 0.0

    @property
    def listings_changed(self) -> int:
        """Listings with at least one tracked field moved (bumps included)."""
        return sum(self.field_changes.values()) + self.listings_bumped

    def render(self) -> str:
        """A fixed-width summary — this is what a scheduled run leaves behind."""
        rule = "─" * 52
        previous = self.previous_snapshot_id or "none"
        lines = [
            "",
            rule,
            f"  snapshot {self.snapshot_id}   mode={self.mode}   previous={previous}",
            rule,
            f"  result pages swept   {self.pages_scanned:>10,}"
            + (f"   ({self.pages_failed} failed)" if self.pages_failed else ""),
            "",
            "  LISTINGS",
            f"    live now           {self.listings_live:>10,}",
            f"    new                {self.listings_new:>10,}"
            + (f"   (+{self.photos_new:,} photos)" if self.photos_new else ""),
            f"    changed            {self.listings_changed:>10,}",
        ]
        for name, count in sorted(
            self.field_changes.items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"      {name:<16} {count:>10,}")
        if self.listings_bumped:
            lines.append(f"      {'bumped':<16} {self.listings_bumped:>10,}")
        if self.listings_content_changed:
            lines.append(
                f"    detail edits       {self.listings_content_changed:>10,}"
            )
        lines += [
            f"    delisted           {self.listings_delisted:>10,}"
            + (f"   [skipped: {self.delisting_skipped}]" if self.delisting_skipped else ""),
            f"    reappeared         {self.listings_reappeared:>10,}",
            "",
            "  SELLERS & COMPLEXES",
            f"    companies changed  {self.companies_changed:>10,}"
            + (f"   (+{self.companies_new} new)" if self.companies_new else ""),
            f"    complexes changed  {self.complexes_changed:>10,}"
            + (f"   (+{self.complexes_new} new)" if self.complexes_new else ""),
            f"    new reviews        {self.reviews_new:>10,}",
            f"    new users          {self.users_new:>10,}",
            rule,
            f"  complete={self.complete}   elapsed={_hms(self.duration_seconds)}",
            rule,
            "",
        ]
        return "\n".join(lines)


class Pipeline:
    """Runs one snapshot. Subclass and override a stage to specialise it."""

    def __init__(self, config: Config, snapshot_id: str | None = None) -> None:
        self.config = config
        paths = config.paths
        self.http = HttpClient(config.http)
        self.storage = Storage(raw_dir=paths.raw, photos_dir=paths.photos)
        self.progress = ProgressTracker(enabled=config.logging.progress)
        self.snapshot_id = snapshot_id or date.today().isoformat()

    # -- entry point -------------------------------------------------------

    def run(self) -> RunReport:
        started = time.perf_counter()
        now = datetime.now(timezone.utc).isoformat()
        baseline = self.config.dataset.is_first_run
        mode = "baseline" if baseline else "refresh"
        previous = None if baseline else self.storage.snapshot_store.previous_of(self.snapshot_id)

        self._announce(mode, previous, baseline)

        report = RunReport(
            snapshot_id=self.snapshot_id, mode=mode, previous_snapshot_id=previous
        )
        differ = Differ(self.snapshot_id, now, previous)
        observations = self.storage.snapshot_store.observations(self.snapshot_id)
        changes = self.storage.snapshot_store.changes(self.snapshot_id)
        self._open_snapshot(now, mode)

        with self.progress:
            sweep = self.collect_urls()
            report.pages_scanned = sweep.pages_scanned
            report.pages_failed = sweep.pages_failed
            report.listings_live = len(sweep.cards)

            new_cards = self.record_sweep(sweep, differ, observations, changes, report, now)
            self.crawl_new_listings(sweep, new_cards, report)
            self.record_delistings(sweep, differ, observations, changes, report, previous, now)
            self.crawl_entities(differ, changes, report)
            self.crawl_users(report)

        report.duration_seconds = time.perf_counter() - started
        report.complete = not sweep.pages_failed and not report.delisting_skipped
        self._close_snapshot(report, now)

        logger.info(report.render())
        if baseline:
            logger.warning(
                "[bold yellow]baseline snapshot complete[/] — set "
                "`dataset.is_first_run: false` in the config so the next run appends "
                "to this dataset instead of rebuilding it"
            )
        return report

    # -- stages ------------------------------------------------------------

    def collect_urls(self) -> Sweep:
        return UrlCollector(self.config, self.http, self.progress).collect()

    def record_sweep(
        self,
        sweep: Sweep,
        differ: Differ,
        observations,
        changes,
        report: RunReport,
        observed_at: str,
    ) -> list[CardObservation]:
        """Store one observation per live listing and diff it against last time.

        Returns the cards whose listings the dataset has never seen, which is what
        stage 2 will spend detail requests on.
        """
        previous_rows = self.storage.snapshot_store.load_observations(
            report.previous_snapshot_id
        )
        known = self.storage.listings.keys
        new_cards: list[CardObservation] = []

        for card in sweep.cards:
            observations.append(self._observation(card, observed_at).to_dict())

            if card.house_kg_id not in known:
                new_cards.append(card)
                changes.append(differ.listing(card, None)[0].to_dict())
                report.listings_new += 1
                continue

            if report.previous_snapshot_id is None:
                # nothing to compare against: this is the first snapshot, or a
                # re-run of it after an interruption. The observation stands on
                # its own; inventing a change here would be noise.
                continue

            previous = previous_rows.get(card.house_kg_id)
            if previous is None:
                # known to the dataset but absent from the previous snapshot
                changes.append(differ.reappeared(card.house_kg_id).to_dict())
                report.listings_reappeared += 1
                continue

            for change in differ.listing(card, previous):
                changes.append(change.to_dict())
                if change.change_type == CHANGE_BUMPED:
                    report.listings_bumped += 1
                elif change.change_type == CHANGE_FIELD and change.field:
                    report.field_changes[change.field] = (
                        report.field_changes.get(change.field, 0) + 1
                    )

        logger.info(
            "sweep recorded: %d observations, %d new, %d reappeared",
            len(sweep.cards), report.listings_new, report.listings_reappeared,
        )
        return new_cards

    def crawl_new_listings(
        self, sweep: Sweep, new_cards: list[CardObservation], report: RunReport
    ) -> None:
        """Detail-crawl what the sweep could not fully describe."""
        force = self.config.refresh.detail_refresh == "all"
        refs = (
            sweep.refs if force else [ListingRef.from_card(c) for c in new_cards]
        )
        if not refs:
            logger.info("no detail pages to fetch")
            return
        counts = ListingCrawler(
            self.config, self.http, self.storage, self.progress
        ).crawl(refs, snapshot_id=self.snapshot_id, force=force)
        report.photos_new = counts["photos"]
        report.listings_content_changed = counts["changed"]

    def record_delistings(
        self,
        sweep: Sweep,
        differ: Differ,
        observations,
        changes,
        report: RunReport,
        previous: str | None,
        observed_at: str,
    ) -> None:
        """Mark listings that were live last snapshot and are gone now.

        Guarded, because the failure mode is severe: a sweep that died half way
        looks exactly like half the board being sold overnight. When the guard
        trips nothing is recorded and the snapshot is flagged incomplete, which is
        recoverable; a false mass-delisting in a published time series is not.
        """
        previously_active = self.storage.snapshot_store.active_listings(previous)
        if not previously_active:
            return

        seen = {card.house_kg_id for card in sweep.cards}
        floor = self.config.refresh.min_completeness * len(previously_active)

        if self.config.scope.max_listings:
            report.delisting_skipped = "run is limited, sweep is partial by design"
        elif len(seen) < floor:
            report.delisting_skipped = (
                f"saw {len(seen):,} of {len(previously_active):,} "
                f"(< {self.config.refresh.min_completeness:.0%})"
            )
        if report.delisting_skipped:
            logger.error(
                "[bold red]delistings NOT recorded[/]: %s", report.delisting_skipped
            )
            return

        for house_kg_id in sorted(previously_active - seen):
            observations.append(
                Observation(
                    snapshot_id=self.snapshot_id,
                    house_kg_id=house_kg_id,
                    observed_at=observed_at,
                    is_active=False,
                ).to_dict()
            )
            changes.append(differ.delisted(house_kg_id).to_dict())
            report.listings_delisted += 1

        logger.info("%d listings went off the board", report.listings_delisted)

    def crawl_entities(self, differ: Differ, changes, report: RunReport) -> None:
        """Entities referenced by every listing we hold (not just this run's)."""
        crawler = EntityCrawler(self.config, self.http, self.storage, self.progress)
        companies: set[str] = set()
        complexes: set[str] = set()
        for row in self.storage.listings.rows():
            if row.get("company_slug"):
                companies.add(row["company_slug"])
            if row.get("complex_slug"):
                complexes.add(row["complex_slug"])

        logger.info(
            "entities referenced: %d companies, %d complexes", len(companies), len(complexes)
        )
        refresh = self.config.refresh.refresh_entities
        for kind, slugs, prefix in (
            ("company", companies, "companies"),
            ("complex", complexes, "complexes"),
        ):
            counts = crawler.crawl_entities(
                kind, slugs, refresh=refresh, differ=differ,
                changes=changes, snapshot_id=self.snapshot_id,
            )
            setattr(report, f"{prefix}_new", counts["new"])
            setattr(report, f"{prefix}_changed", counts["changed"])
            report.reviews_new += counts["reviews"]

    def crawl_users(self, report: RunReport) -> None:
        crawler = EntityCrawler(self.config, self.http, self.storage, self.progress)
        ad_authors = {
            row["author_user_id"]
            for row in self.storage.listings.rows()
            if row.get("author_user_id")
        }
        reviewers = {
            row["user_id"] for row in self.storage.reviews.rows() if row.get("user_id")
        }
        report.users_new = crawler.crawl_users(
            ad_authors, reviewers, refresh=self.config.refresh.refresh_users
        )

    # -- snapshot bookkeeping ---------------------------------------------

    def _announce(self, mode: str, previous: str | None, baseline: bool) -> None:
        scope = self.config.scope
        logger.info(
            "[bold cyan]house.kg %s[/] snapshot=%s — deals=%s types=%d regions=%s",
            mode, self.snapshot_id, ",".join(scope.deals),
            len(scope.property_types), ",".join(scope.regions),
        )
        existing = self.storage.snapshot_store.snapshot_ids()
        if baseline and existing:
            logger.warning(
                "[bold yellow]is_first_run is true but %d snapshot(s) already exist "
                "locally (%s)[/] — this run will not diff against them",
                len(existing), ", ".join(existing[-3:]),
            )
        elif not baseline and previous is None:
            logger.warning(
                "no previous snapshot found — everything will be recorded as new. "
                "If this dataset lives on the Hub, run `house-kg pull` first."
            )
        before = self.storage.summary()
        if any(before.values()):
            logger.info("existing data: %s", before)

    def _open_snapshot(self, started_at: str, mode: str) -> None:
        scope = self.config.scope
        self.storage.snapshots.upsert(
            Snapshot(
                snapshot_id=self.snapshot_id,
                started_at=started_at,
                mode=mode,
                scope_deals=",".join(scope.deals),
                scope_types=",".join(scope.property_types),
                scope_regions=",".join(scope.regions),
            ).to_dict()
        )

    def _close_snapshot(self, report: RunReport, started_at: str) -> None:
        self.storage.snapshots.upsert(
            Snapshot(
                snapshot_id=self.snapshot_id,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                mode=report.mode,
                complete=report.complete,
                scope_deals=",".join(self.config.scope.deals),
                scope_types=",".join(self.config.scope.property_types),
                scope_regions=",".join(self.config.scope.regions),
                pages_scanned=report.pages_scanned,
                listings_seen=report.listings_live,
                listings_new=report.listings_new,
                listings_changed=report.listings_changed,
                listings_delisted=report.listings_delisted,
                listings_reappeared=report.listings_reappeared,
                photos_new=report.photos_new,
                entities_changed=report.companies_changed + report.complexes_changed,
                reviews_new=report.reviews_new,
                duration_seconds=round(report.duration_seconds, 1),
            ).to_dict()
        )

    def _observation(self, card: CardObservation, observed_at: str) -> Observation:
        """Card -> one row of the panel.

        `views` and `favourites` ride along even though nothing diffs them: they
        cost no extra request, and view velocity is exactly the kind of question
        a time series is built to answer.
        """
        return Observation(
            snapshot_id=self.snapshot_id,
            house_kg_id=card.house_kg_id,
            observed_at=observed_at,
            price_usd=card.price_usd,
            price_kgs=card.price_kgs,
            price_period=card.price_period,
            price_usd_per_m2=card.price_usd_per_m2,
            views=card.views,
            favourites=card.favourites,
            upped_date=card.upped_date,
            promo=card.promo,
            is_vip=card.is_vip,
            is_premium=card.is_premium,
            is_top=card.is_top,
            is_urgent=card.is_urgent,
            owner_badge=card.owner_badge,
            is_active=True,
        )


def _hms(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
