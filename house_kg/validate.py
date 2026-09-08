"""Integrity checks over the scraped tables.

Run after a crawl and before publishing. These are the checks that caught real
bugs during development — a mis-detected seller type, a rent price typed as a
sale total, foreign keys pointing at entities that were never crawled.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .constants import REVIEW_CAP
from .logging_utils import get_logger
from .models import Listing
from .storage import Storage

logger = get_logger(__name__)


@dataclass(slots=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


class Validator:
    """Asserts the invariants the dataset promises its users."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.checks: list[Check] = []

    def _check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name, passed, detail))
        level = logger.info if passed else logger.error
        level("  [%s] %s %s", "OK  " if passed else "FAIL", name, detail)

    def run(self) -> bool:
        # `latest()`, not `rows()`: these tables keep every version of a record, so
        # the current state is the last row per key — `rows()` would report the
        # history as duplicate primary keys.
        listings = list(self.storage.listings.latest())
        users = list(self.storage.users.latest())
        companies = list(self.storage.companies.latest())
        complexes = list(self.storage.complexes.latest())
        reviews = list(self.storage.reviews.rows())
        photos = list(self.storage.photos.rows())
        snapshots = list(self.storage.snapshots.latest())

        if not listings:
            logger.error("no listings to validate")
            return False

        logger.info(
            "validating: %d listings, %d users, %d companies, %d complexes, "
            "%d reviews, %d photos",
            len(listings), len(users), len(companies), len(complexes),
            len(reviews), len(photos),
        )

        logger.info("[bold]primary keys[/]")
        self._check("listing id unique", _unique(listings, "id"))
        self._check("listing house_kg_id unique", _unique(listings, "house_kg_id"))
        self._check("user_id unique", _unique(users, "user_id"))
        self._check("company slug unique", _unique(companies, "slug"))
        self._check("complex slug unique", _unique(complexes, "slug"))
        self._check("review_id unique", _unique(reviews, "review_id"))
        self._check(
            "review_id is a deterministic hash (not uuid4)",
            all(len(r["review_id"]) == 16 and "-" not in r["review_id"] for r in reviews),
        )
        # A random id would change on every re-read and silently orphan the photos
        # of any listing crawled twice — the whole time series rests on this.
        self._check(
            "listing.id is derived from house_kg_id (reproducible across snapshots)",
            all(r["id"] == Listing.make_id(r["house_kg_id"]) for r in listings),
        )

        logger.info("[bold]foreign keys[/]")
        company_slugs = {c["slug"] for c in companies}
        complex_slugs = {c["slug"] for c in complexes}

        # Companies and complexes each key on `slug`, and they live in different URL
        # namespaces (/<slug> vs /jilie-kompleksy/<slug>) — so the same string CAN
        # name both, and does: the agency «Дипломат» and the complex «Дипломат» are
        # both `diplomat`.
        #
        # This is NOT a defect: the tables are separate and every FK is unambiguous
        # (listings.company_slug only ever points at companies; reviews carry
        # subject_type). It is a warning rather than a failure because the data is
        # sound — but anyone who concatenates the two entity tables on `slug` alone
        # would silently merge an agency with a building, so it must be surfaced.
        collisions = company_slugs & complex_slugs
        if collisions:
            logger.warning(
                "  [NOTE] %d slug(s) name BOTH a company and a complex: %s. "
                "The tables are separate and all FKs stay unambiguous, but never "
                "join companies and complexes on `slug` alone.",
                len(collisions), sorted(collisions)[:5],
            )
        else:
            logger.info("  [OK  ] no slug names both a company and a complex")
        user_ids = {u["user_id"] for u in users}
        listing_ids = {r["id"] for r in listings}

        self._check(
            "listings.company_slug resolves",
            _resolves(listings, "company_slug", company_slugs),
        )
        self._check(
            "listings.complex_slug resolves",
            _resolves(listings, "complex_slug", complex_slugs),
        )
        self._check(
            "listings.author_user_id resolves",
            _resolves(listings, "author_user_id", user_ids),
        )
        self._check("reviews.user_id resolves", _resolves(reviews, "user_id", user_ids))
        self._check("photos.listing_id resolves", _resolves(photos, "listing_id", listing_ids))

        bad_subjects = [
            r
            for r in reviews
            if r["subject_slug"]
            not in (company_slugs if r["subject_type"] == "company" else complex_slugs)
        ]
        self._check(
            "reviews.subject_slug resolves", not bad_subjects, f"bad={len(bad_subjects)}"
        )

        logger.info("[bold]photos[/]")
        on_disk = self.storage.photo_store.existing()
        missing = [p for p in photos if p["file_name"] not in on_disk.values()]
        self._check("every photo row has a file", not missing, f"missing={len(missing)}")
        self._check("no duplicate foto_id", _unique(photos, "foto_id"))

        logger.info("[bold]price semantics[/]")
        self._check(
            "sale price is always a total",
            all(r["price_period"] == "total" for r in listings if r["deal"] == "sale"),
        )
        self._check(
            "rent price is never a total",
            all(r["price_period"] != "total" for r in listings if r["deal"] == "rent"),
            f"periods={dict(Counter(r['price_period'] for r in listings))}",
        )

        logger.info("[bold]seller: declared vs actual[/]")
        cross = Counter((r.get("offer_type"), r["seller_type"]) for r in listings)
        for (declared, actual), n in cross.most_common():
            mismatch = declared and ("собственник" in declared) != (actual == "owner")
            logger.info(
                "  %-18s -> %-8s %5d%s",
                declared, actual, n, "   <- mismatch" if mismatch else "",
            )
        self._check(
            "seller_mismatch flag agrees with the cross-tab",
            sum(1 for r in listings if r.get("seller_mismatch"))
            == sum(
                n
                for (declared, actual), n in cross.items()
                if declared and ("собственник" in declared) != (actual == "owner")
            ),
        )

        logger.info("[bold]time series[/]")
        self._check("snapshot_id unique", _unique(snapshots, "snapshot_id"))

        snapshot_ids = {s["snapshot_id"] for s in snapshots}
        listing_keys = {r["house_kg_id"] for r in listings}
        store = self.storage.snapshot_store
        observed_snapshots = store.snapshot_ids()

        self._check(
            "every observation partition has a snapshots row",
            set(observed_snapshots) <= snapshot_ids,
            f"orphans={sorted(set(observed_snapshots) - snapshot_ids)}",
        )

        # An observation without a listing row is normal *in the newest snapshot*:
        # the card was read, then the detail fetch failed, and the next run retries
        # it. In an older snapshot it never healed, so it is a real defect.
        settled = observed_snapshots[:-1]
        stale_orphans = sum(
            1
            for snapshot_id in settled
            for key in store.load_observations(snapshot_id)
            if key not in listing_keys
        )
        pending_orphans = sum(
            1
            for key in store.load_observations(observed_snapshots[-1])
            if key not in listing_keys
        ) if observed_snapshots else 0

        self._check(
            "every settled observation resolves to a listing",
            not stale_orphans,
            f"orphans={stale_orphans}",
        )
        if pending_orphans:
            logger.warning(
                "  [NOTE] %d observation(s) in the newest snapshot have no listing "
                "row yet — their detail page failed and the next run will retry",
                pending_orphans,
            )

        # A listing may only be observed as delisted once: after that it stops
        # appearing entirely, so a second delisting means the active-set logic drifted.
        delisted_twice: Counter[str] = Counter()
        for snapshot_id in observed_snapshots:
            for key, row in store.load_observations(snapshot_id).items():
                if not row.get("is_active", True):
                    delisted_twice[key] += 1
        repeats = [k for k, n in delisted_twice.items() if n > 1]
        self._check(
            "no listing is delisted twice", not repeats, f"repeats={len(repeats)}"
        )

        incomplete = [s["snapshot_id"] for s in snapshots if not s.get("complete")]
        if incomplete:
            logger.warning(
                "  [NOTE] %d snapshot(s) are flagged incomplete: %s. Their "
                "observations are partial and they record no delistings — treat "
                "them as gaps, not as market movements.",
                len(incomplete), ", ".join(incomplete[:5]),
            )

        logger.info("[bold]review completeness[/]")
        entities = companies + complexes
        capped = [e for e in entities if e.get("reviews_truncated")]
        self._check(
            f"truncation only ever occurs at the site's {REVIEW_CAP}-review cap",
            all(e["reviews_scraped"] == REVIEW_CAP for e in capped),
            f"capped={len(capped)}/{len(entities)}",
        )

        passed = all(c.passed for c in self.checks)
        failed = [c.name for c in self.checks if not c.passed]
        if passed:
            logger.info("[bold green]ALL %d CHECKS PASSED[/]", len(self.checks))
        else:
            logger.error("[bold red]%d CHECK(S) FAILED:[/] %s", len(failed), ", ".join(failed))
        return passed


def _unique(rows: list[dict], key: str) -> bool:
    values = [r[key] for r in rows if r.get(key) is not None]
    return len(values) == len(set(values))


def _resolves(rows: list[dict], key: str, universe: set) -> bool:
    return all(r[key] in universe for r in rows if r.get(key))
