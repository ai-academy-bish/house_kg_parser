"""Stage 1 — sweep the result pages.

This stage does double duty. It discovers listing URLs, as it always did, but it
now also returns a full observation per card: price, views, favourites, bump and
paid-promotion state all render on the card itself. That is what makes a repeat
run cheap — the whole board is re-measured in ~2.6k page fetches, and detail
pages are reserved for advertisements never seen before.

The sweep is also the only way to learn what *disappeared*: a listing that no
longer shows up in any stream has been sold or withdrawn, and nothing on the
site announces that.

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
from ..models import CardObservation
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

    @classmethod
    def from_card(cls, card: CardObservation) -> ListingRef:
        return cls(card.source_url, card.deal, card.type, card.region)


@dataclass(slots=True)
class Sweep:
    """What one pass over the result pages found."""

    cards: list[CardObservation]
    pages_scanned: int
    pages_expected: int = 0
    #: Pages that returned nothing — a transport failure, not an empty stream.
    pages_failed: int = 0

    @property
    def refs(self) -> list[ListingRef]:
        return [ListingRef.from_card(c) for c in self.cards]


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

    def collect(self) -> Sweep:
        """Walk every stream's pages and return one observation per live listing.

        Deduplication matters: a listing bumped mid-crawl can shift pages and be
        served twice. The first sighting wins, so a listing keeps the deal and
        type of the stream that found it.
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

        # 2) read every card off every page
        cards: list[CardObservation] = []
        seen: set[str] = set()
        limit = self.config.scope.max_listings
        scanned = 0
        failed = 0

        self.progress.track("urls", len(jobs), "sweeping result pages")
        with ThreadPoolExecutor(max_workers=self.config.http.workers) as pool:
            card_futures = {
                pool.submit(self._page_cards, stream, page): stream for stream, page in jobs
            }
            for future in as_completed(card_futures):
                page_cards = future.result()
                scanned += 1
                if page_cards is None:
                    failed += 1
                    self.progress.advance("urls")
                    continue
                for card in page_cards:
                    if card.house_kg_id not in seen:
                        seen.add(card.house_kg_id)
                        cards.append(card)
                self.progress.advance("urls")
                if limit and len(cards) >= limit:
                    for pending in card_futures:
                        pending.cancel()
                    break

        self.progress.complete("urls")
        if limit:
            cards = cards[:limit]
        if failed:
            logger.warning("%d of %d result pages could not be fetched", failed, len(jobs))
        logger.info("swept %d pages -> %d unique live listings", scanned, len(cards))
        return Sweep(
            cards=cards, pages_scanned=scanned, pages_expected=len(jobs), pages_failed=failed
        )

    def _page_cards(self, stream: Stream, page: int) -> list[CardObservation] | None:
        """None marks a page that could not be fetched, so it is not read as empty."""
        html = self.http.get_text(stream.page_url(page))
        if not html:
            return None
        return self.parser.cards(html, stream.deal, stream.property_type, stream.region)
