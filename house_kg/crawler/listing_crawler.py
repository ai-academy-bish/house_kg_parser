"""Stage 2 — fetch and parse listing detail pages, and download their photos.

On a repeat run this stage is reserved for advertisements never seen before: the
volatile fields were already read off the result card, and a detail page costs a
full request plus ~9 photo downloads. `force=True` re-reads known listings too,
which is what `refresh.detail_refresh: all` uses to pick up edited descriptions
and characteristics.

Photos are fetched once per listing and never again. They are the entire weight
of the dataset (~46 GB for a full board), and an advertisement's images do not
change often enough to justify re-downloading 230 000 files a week.
"""

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
        self.snapshot_id = ""
        #: house_kg_id -> foto_ids already on disk (filled per crawl).
        self._known_photos: dict[str, list[str]] = {}

    def crawl(
        self, refs: list[ListingRef], snapshot_id: str = "", force: bool = False
    ) -> dict[str, int]:
        """Crawl the given refs. Returns counts of new / changed / photos."""
        self.snapshot_id = snapshot_id
        pending = (
            list(refs)
            if force
            else [r for r in refs if self._key(r.url) not in self.storage.listings]
        )
        skipped = len(refs) - len(pending)
        if skipped:
            logger.info("skipping %d listings already stored (resume)", skipped)
        if not pending:
            logger.info("nothing to crawl — listings are up to date")
            return {"new": 0, "changed": 0, "photos": 0}

        # Photos already on disk. Re-downloading them is by far the most expensive
        # mistake this crawler could make, and the ids must be carried onto the new
        # version of the listing or a re-read would blank its `foto_ids`.
        self._known_photos = {}
        if self.config.photos.enabled:
            for row in self.storage.photos.rows():
                key = str(row.get("house_kg_id"))
                self._known_photos.setdefault(key, []).append(str(row.get("foto_id")))

        logger.info("crawling %d listings with %d workers", len(pending), self.config.http.workers)
        self.progress.track("listings", len(pending), "listings")
        # a listing's photo count is only known once its page is parsed, so this
        # track has no total up front and simply counts up
        self.progress.track("photos", None, "photos")

        counts = {"new": 0, "changed": 0, "photos": 0}
        with ThreadPoolExecutor(max_workers=self.config.http.workers) as pool:
            futures = [pool.submit(self._one, ref) for ref in pending]
            for future in as_completed(futures):
                try:
                    status, photos = future.result()
                    if status in counts:
                        counts[status] += 1
                    counts["photos"] += photos
                except Exception:  # one bad page must not kill the crawl
                    logger.exception("listing failed")
                finally:
                    self.progress.advance("listings")

        self.progress.complete("listings")
        logger.info(
            "stored %d new listings, %d updated, %d photos",
            counts["new"], counts["changed"], counts["photos"],
        )
        return counts

    @staticmethod
    def _key(url: str) -> str:
        return url.rstrip("/").split("/details/")[-1]

    def _one(self, ref: ListingRef) -> tuple[str, int]:
        html = self.http.get_text(ref.url)
        if not html:
            return "failed", 0

        listing = self.parser.parse(
            html, ref.url, ref.deal, ref.property_type, ref.region
        )

        downloaded = 0
        existing = self._known_photos.get(listing.house_kg_id)
        if existing:
            listing.foto_ids = list(existing)
        elif self.config.photos.enabled:
            urls = self.parser.photo_urls(html)
            cap = self.config.photos.max_per_listing
            if cap:
                urls = urls[:cap]
            downloaded = self._download_photos(listing, urls)

        row = listing.to_dict()
        row["first_seen"] = listing.pars_date
        row["first_snapshot"] = self.snapshot_id
        status, _ = self.storage.listings.upsert(row)
        return status, downloaded

    def _download_photos(self, listing: Listing, urls: list[str]) -> int:
        """Fetch a listing's photos; each becomes a row in the `photos` table."""
        downloaded = 0
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
                    snapshot_id=self.snapshot_id,
                ).to_dict()
            )
            downloaded += 1
            self.progress.advance("photos")
        return downloaded
