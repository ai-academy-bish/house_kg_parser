"""Stage 2 — fetch and parse listing detail pages, and download their photos."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import Config
from ..http_client import HttpClient
from ..logging_utils import ProgressTracker, get_logger
from ..models import Listing, Photo
from ..parsers import ListingParser
from ..storage import Storage
from .url_collector import ListingRef

logger = get_logger(__name__)


class ListingCrawler:
    """Parses listings and persists each one immediately.

    Records are appended to JSONL as they complete rather than collected in memory,
    so an interrupted 3-hour crawl keeps everything it had already done.
    """

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
        self.parser = ListingParser()

    def crawl(self, refs: list[ListingRef]) -> int:
        """Crawl the given refs, skipping anything a previous run already stored."""
        pending = [r for r in refs if self._key(r.url) not in self.storage.listings]
        skipped = len(refs) - len(pending)
        if skipped:
            logger.info("skipping %d listings already stored (resume)", skipped)
        if not pending:
            logger.info("nothing to crawl — listings are up to date")
            return 0

        logger.info("crawling %d listings with %d workers", len(pending), self.config.http.workers)
        self.progress.track("listings", len(pending), "listings")
        # a listing's photo count is only known once its page is parsed, so this
        # track has no total up front and simply counts up
        self.progress.track("photos", None, "photos")

        written = 0
        with ThreadPoolExecutor(max_workers=self.config.http.workers) as pool:
            futures = [pool.submit(self._one, ref) for ref in pending]
            for future in as_completed(futures):
                try:
                    if future.result():
                        written += 1
                except Exception:  # one bad page must not kill the crawl
                    logger.exception("listing failed")
                finally:
                    self.progress.advance("listings")

        self.progress.complete("listings")
        logger.info("stored %d new listings", written)
        return written

    @staticmethod
    def _key(url: str) -> str:
        return url.rstrip("/").split("/details/")[-1]

    def _one(self, ref: ListingRef) -> bool:
        html = self.http.get_text(ref.url)
        if not html:
            return False

        listing = self.parser.parse(
            html, ref.url, ref.deal, ref.property_type, ref.region
        )

        if self.config.photos.enabled:
            urls = self.parser.photo_urls(html)
            cap = self.config.photos.max_per_listing
            if cap:
                urls = urls[:cap]
            self._download_photos(listing, urls)

        return self.storage.listings.append(listing.to_dict())

    def _download_photos(self, listing: Listing, urls: list[str]) -> None:
        """Fetch a listing's photos; each becomes a row in the `photos` table."""
        for url in urls:
            data = self.http.get_bytes(url)
            if not data:
                continue
            foto_id, path = self.storage.photo_store.save(data, url)
            listing.foto_ids.append(foto_id)
            self.storage.photos.append(
                Photo(
                    foto_id=foto_id,
                    listing_id=listing.id,
                    house_kg_id=listing.house_kg_id,
                    file_name=path.name,
                    url=url,
                ).to_dict()
            )
            self.progress.advance("photos")
