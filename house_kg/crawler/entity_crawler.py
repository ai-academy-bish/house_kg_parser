"""Stage 3 — crawl the entities the listings point at.

Companies, complexes and users are *shared*: in a 1000-listing sample, ~590
listings pointed at only ~70 companies. Crawling them once from their own profile
pages, instead of copying their reviews into every listing, cuts roughly an
order of magnitude of duplicated work and storage.

On a repeat run companies and complexes are re-read *in full* rather than skipped.
There are only ~900 of them, their ratings and review counts genuinely move, and
that movement is one of the things the time series exists to capture. Users are
the opposite case — ~4 500 profiles that rarely change — so they stay
new-only unless `refresh.refresh_users` says otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import Config
from ..http_client import HttpClient
from ..logging_utils import ProgressTracker, get_logger
from ..parsers import EntityParser, UserParser
from ..storage import JsonlTable, Storage
from .differ import Differ

logger = get_logger(__name__)


class EntityCrawler:
    """Crawls companies, complexes and users referenced by stored listings."""

    def __init__(
        self,
        config: Config,
        http: HttpClient,
        storage: Storage,
        progress: ProgressTracker,
    ) -> None:
        self.config = config
        self.http = http
        self.storage = storage
        self.progress = progress
        self.entities = EntityParser()
        self.users = UserParser()

    # -- companies & complexes --------------------------------------------

    def crawl_entities(
        self,
        kind: str,
        slugs: Iterable[str],
        refresh: bool = False,
        differ: Differ | None = None,
        changes: JsonlTable | None = None,
        snapshot_id: str = "",
    ) -> dict[str, int]:
        """Crawl company / complex profiles.

        `refresh` re-reads profiles already stored, so a rating that moved is
        recorded instead of skipped. Returns counts of new / changed / reviews.
        """
        table = self.storage.companies if kind == "company" else self.storage.complexes
        wanted = {s for s in slugs if s}
        pending = sorted(wanted if refresh else wanted - table.keys)
        if not pending:
            logger.info("no %s entities to crawl", kind)
            return {"new": 0, "changed": 0, "reviews": 0}

        track = "companies" if kind == "company" else "complexes"
        logger.info(
            "crawling %d %s profiles%s", len(pending), kind, " (refresh)" if refresh else ""
        )
        self.progress.track(track, len(pending), f"{kind} profiles")

        counts = {"new": 0, "changed": 0, "reviews": 0}
        with ThreadPoolExecutor(max_workers=self.config.http.workers) as pool:
            futures = {
                pool.submit(self._one_entity, kind, slug, differ, changes, snapshot_id): slug
                for slug in pending
            }
            for future in as_completed(futures):
                try:
                    status, reviews_new = future.result()
                    if status in counts:
                        counts[status] += 1
                    counts["reviews"] += reviews_new
                except Exception:
                    logger.exception("entity failed: %s", futures[future])
                finally:
                    self.progress.advance(track)

        self.progress.complete(track)
        return counts

    def _one_entity(
        self,
        kind: str,
        slug: str,
        differ: Differ | None = None,
        changes: JsonlTable | None = None,
        snapshot_id: str = "",
    ) -> tuple[str, int]:
        url = self.entities.url_for(kind, slug)
        html = self.http.get_text(url)
        if not html:
            return "failed", 0

        entity, reviews = self.entities.parse(html, kind, slug)
        table = self.storage.companies if kind == "company" else self.storage.complexes

        row = entity.to_dict()
        row["first_seen"] = entity.pars_date
        row["first_snapshot"] = snapshot_id
        status, diffs = table.upsert(row)
        if diffs and differ and changes:
            for change in differ.entity(kind, slug, diffs):
                changes.append(change.to_dict())

        # reviews go to their own table, keyed by a deterministic hash, so a
        # review seen again is recognised rather than duplicated under a new id
        reviews_new = 0
        for review in reviews:
            review_row = review.to_dict()
            review_row["first_snapshot"] = snapshot_id
            if self.storage.reviews.append(review_row):
                reviews_new += 1
                if differ and changes:
                    changes.append(differ.review_added(review_row).to_dict())

        if entity.rating.truncated:
            logger.debug(
                "%s/%s: only %d of %d reviews available (site cap)",
                kind, slug, entity.rating.scraped, entity.rating.count,
            )
        return status, reviews_new

    # -- users -------------------------------------------------------------

    def crawl_users(
        self, ad_authors: set[str], reviewers: set[str], refresh: bool = False
    ) -> int:
        """Crawl the UNION of listing authors and review authors.

        Both share the `/user/<hash>` namespace, so a person who posts ads *and*
        writes reviews is one row, flagged on both counts.
        """
        wanted = {u for u in (ad_authors | reviewers) if u}
        pending = sorted(wanted if refresh else wanted - self.storage.users.keys)
        if not pending:
            logger.info("no new users to crawl")
            return 0

        logger.info(
            "crawling %d users (%d ad authors + %d reviewers, %d already stored)",
            len(pending), len(ad_authors), len(reviewers), len(wanted) - len(pending),
        )
        self.progress.track("users", len(pending), "user profiles")

        written = 0
        with ThreadPoolExecutor(max_workers=self.config.http.workers) as pool:
            futures = {
                pool.submit(self._one_user, uid, uid in ad_authors, uid in reviewers): uid
                for uid in pending
            }
            for future in as_completed(futures):
                try:
                    if future.result():
                        written += 1
                except Exception:
                    logger.exception("user failed: %s", futures[future])
                finally:
                    self.progress.advance("users")

        self.progress.complete("users")
        return written

    def _one_user(self, user_id: str, is_ad_author: bool, is_reviewer: bool) -> bool:
        html = self.http.get_text(self.users.url_for(user_id))
        if not html:
            return False
        user = self.users.parse(html, user_id)
        user.is_ad_author = is_ad_author
        user.is_reviewer = is_reviewer
        status, _ = self.storage.users.upsert(user.to_dict())
        return status != "unchanged"
