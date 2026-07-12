"""Stage 1 — discover listing URLs.

The site is crawled per (deal × property type × region) stream rather than through
`?region=all`, for two reasons:

* `region=all` also returns Russia, Kazakhstan, UAE... — countries we must exclude;
* the stream URL *tells* us the deal, the type and the region, so all three are
  known for free and are never guessed from page text.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from ..config import Config
from ..constants import BASE_URL, DEALS, REGION_IDS_BY_NAME
from ..http_client import HttpClient
from ..logging_utils import ProgressTracker, get_logger
from ..parsers import ResultsParser

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Stream:
    """One (deal, type, region) crawl stream."""

    deal: str
    property_type: str
    slug: str
    region: str
    region_id: int

    def page_url(self, page: int) -> str:
        return f"{BASE_URL}/{self.slug}?region={self.region_id}&page={page}"


@dataclass(frozen=True, slots=True)
class ListingRef:
    """A listing URL together with the classification its stream implies."""

    url: str
    deal: str
    property_type: str
    region: str


class UrlCollector:
    """Enumerates every listing URL within the configured scope."""

    def __init__(self, config: Config, http: HttpClient, progress: ProgressTracker) -> None:
        self.config = config
        self.http = http
        self.progress = progress
        self.parser = ResultsParser()

    def streams(self) -> list[Stream]:
        scope = self.config.scope
        out: list[Stream] = []
        for deal in scope.deals:
            for property_type in scope.property_types:
                slug = DEALS[deal][property_type]
                for region in scope.regions:
                    out.append(
                        Stream(
                            deal=deal,
                            property_type=property_type,
                            slug=slug,
                            region=region,
                            region_id=REGION_IDS_BY_NAME[region],
                        )
                    )
        return out

    def _page_count(self, stream: Stream) -> int:
        html = self.http.get_text(stream.page_url(1))
        if not html:
            return 0
        last = self.parser.last_page(html)
        cap = self.config.scope.max_pages_per_stream
        return min(last, cap) if cap else last

    def collect(self) -> list[ListingRef]:
        """Walk every stream's pages and return de-duplicated listing refs.

        Deduplication matters: a listing bumped mid-crawl can shift pages and be
        served twice.
        """
        streams = self.streams()
        logger.info("scope: %d streams (deal × type × region)", len(streams))

        # 1) how many pages does each stream have?
        page_counts: dict[Stream, int] = {}
        with (
            self.progress.stage("urls", len(streams), "sizing streams"),
            ThreadPoolExecutor(max_workers=self.config.http.workers) as pool,
        ):
            futures = {pool.submit(self._page_count, s): s for s in streams}
            for future in as_completed(futures):
                page_counts[futures[future]] = future.result()
                self.progress.advance("urls")

        jobs = [
            (stream, page)
            for stream, pages in page_counts.items()
            for page in range(1, pages + 1)
        ]
        total_estimate = sum(page_counts.values())
        logger.info(
            "%d result pages to scan (~%d listings)",
            total_estimate,
            total_estimate * 10,
        )

        # 2) pull the URLs off every page
        refs: list[ListingRef] = []
        seen: set[str] = set()
        limit = self.config.scope.max_listings

        self.progress.track("urls", len(jobs), "collecting urls")
        with ThreadPoolExecutor(max_workers=self.config.http.workers) as pool:
            futures = {
                pool.submit(self._page_refs, stream, page): stream for stream, page in jobs
            }
            for future in as_completed(futures):
                for ref in future.result():
                    if ref.url not in seen:
                        seen.add(ref.url)
                        refs.append(ref)
                self.progress.advance("urls")
                if limit and len(refs) >= limit:
                    for pending in futures:
                        pending.cancel()
                    break

        self.progress.complete("urls")
        if limit:
            refs = refs[:limit]
        logger.info("collected %d unique listing urls", len(refs))
        return refs

    def _page_refs(self, stream: Stream, page: int) -> list[ListingRef]:
        html = self.http.get_text(stream.page_url(page))
        if not html:
            return []
        return [
            ListingRef(url, stream.deal, stream.property_type, stream.region)
            for url in self.parser.listing_urls(html)
        ]
