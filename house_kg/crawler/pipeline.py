"""The crawl pipeline: URLs → listings (+photos) → entities → users.

Ordering is deliberate. Listings are crawled first because they are what *discover*
the entities: only once a listing is parsed do we know which company, complex and
user it points at. Entities are then crawled once each, from the union of every
reference seen — a few hundred pages instead of tens of thousands.

The whole pipeline is resumable: each stage reads what is already in storage and
crawls only the remainder, so a run killed at 80% picks up at 80%.
"""

from __future__ import annotations

import time

from ..config import Config
from ..http_client import HttpClient
from ..logging_utils import ProgressTracker, get_logger
from ..storage import Storage
from .entity_crawler import EntityCrawler
from .listing_crawler import ListingCrawler
from .url_collector import UrlCollector

logger = get_logger(__name__)


class Pipeline:
    """Runs the full crawl. Subclass and override a stage to specialise it."""

    def __init__(self, config: Config) -> None:
        self.config = config
        paths = config.paths
        self.http = HttpClient(config.http)
        self.storage = Storage(raw_dir=paths.raw, photos_dir=paths.photos)
        self.progress = ProgressTracker(enabled=config.logging.progress)

    def run(self) -> dict[str, int]:
        started = time.perf_counter()
        logger.info(
            "[bold cyan]house.kg crawl[/] — deals=%s types=%d regions=%s",
            ",".join(self.config.scope.deals),
            len(self.config.scope.property_types),
            ",".join(self.config.scope.regions),
        )
        before = self.storage.summary()
        if any(before.values()):
            logger.info("existing data: %s", before)

        with self.progress:
            refs = self.collect_urls()
            self.crawl_listings(refs)
            self.crawl_entities()
            self.crawl_users()

        after = self.storage.summary()
        elapsed = time.perf_counter() - started
        self._report(before, after, elapsed)
        return after

    # -- stages ------------------------------------------------------------

    def collect_urls(self):
        return UrlCollector(self.config, self.http, self.progress).collect()

    def crawl_listings(self, refs) -> None:
        ListingCrawler(self.config, self.http, self.storage, self.progress).crawl(refs)

    def crawl_entities(self) -> None:
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
        crawler.crawl_entities("company", companies)
        crawler.crawl_entities("complex", complexes)

    def crawl_users(self) -> None:
        crawler = EntityCrawler(self.config, self.http, self.storage, self.progress)
        ad_authors = {
            row["author_user_id"]
            for row in self.storage.listings.rows()
            if row.get("author_user_id")
        }
        reviewers = {
            row["user_id"] for row in self.storage.reviews.rows() if row.get("user_id")
        }
        crawler.crawl_users(ad_authors, reviewers)

    # -- reporting ---------------------------------------------------------

    def _report(self, before: dict[str, int], after: dict[str, int], elapsed: float) -> None:
        logger.info("[bold green]crawl complete[/] in %s", _hms(elapsed))
        for table, total in after.items():
            added = total - before.get(table, 0)
            logger.info("  %-10s %7d  (+%d)", table, total, added)

        added_listings = after["listings"] - before.get("listings", 0)
        if added_listings and elapsed > 0:
            logger.info("  rate       %7.0f listings/hour", added_listings / elapsed * 3600)


def _hms(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
