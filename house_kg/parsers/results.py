"""Result-page parser: listing URLs, pagination, and full card observations.

A result card carries every field that *moves* between runs — price, views,
favourites, the bump marker and the paid-promotion badges. That is the whole
basis of the incremental design: a refresh pass reads ~2.6k result pages instead
of ~25k detail pages, and downloads no photos at all. Detail pages are then
fetched only for advertisements never seen before.

Static fields (coordinates, characteristics, author, description) are NOT read
here — the card does not carry them, and they do not change often enough to be
worth 25k requests a week.
"""

from __future__ import annotations

import re

from bs4 import Tag

from ..constants import BASE_URL, Selectors
from ..models import CardObservation
from ..utils import clean_text, parse_price, to_int
from .base import BaseParser


class ResultsParser(BaseParser):
    """Reads a `/kupit-*?region=N&page=M` result page."""

    def listing_urls(self, html: str) -> list[str]:
        soup = self.soup(html)
        urls: list[str] = []
        for card in soup.select(Selectors.LISTING_CARD):
            href = self.attr(card.select_one(Selectors.CARD_LINK), "href")
            if href:
                urls.append(BASE_URL + href.split("?")[0])
        return urls

    def last_page(self, html: str) -> int:
        """Page number behind the «Последняя» link (1 when there is no pagination)."""
        soup = self.soup(html)
        for anchor in soup.select(Selectors.PAGINATION):
            if "Последняя" in anchor.get_text():
                m = re.search(r"page=(\d+)", self.attr(anchor, "href") or "")
                if m:
                    return int(m.group(1))
        return 1 if soup.select(Selectors.LISTING_CARD) else 0

    # -- observations ------------------------------------------------------

    def cards(
        self, html: str, deal: str, property_type: str, region: str
    ) -> list[CardObservation]:
        """Every card on the page, as a ready-to-store observation."""
        soup = self.soup(html)
        out: list[CardObservation] = []
        for card in soup.select(Selectors.LISTING_CARD):
            observation = self.parse_card(card, deal, property_type, region)
            if observation is not None:
                out.append(observation)
        return out

    def parse_card(
        self, card: Tag, deal: str, property_type: str, region: str
    ) -> CardObservation | None:
        """One card. Returns None if it carries no listing link (an ad slot)."""
        href = self.attr(card.select_one(Selectors.CARD_LINK), "href")
        if not href or "/details/" not in href:
            return None

        house_kg_id = href.split("?")[0].rstrip("/").split("/details/")[-1]
        now = self.now()

        usd_raw = clean_text(self.text_of(card, Selectors.CARD_PRICE_MAIN))
        kgs_raw = clean_text(self.text_of(card, Selectors.CARD_PRICE_MAIN_ADD))
        posted_raw, upped_raw = self._card_dates(card)
        promo = self._promo(card)

        return CardObservation(
            house_kg_id=house_kg_id,
            source_url=f"{BASE_URL}/details/{house_kg_id}",
            deal=deal,
            type=property_type,
            region=region,
            price_usd_raw=usd_raw,
            price_kgs_raw=kgs_raw,
            price_usd=parse_price(usd_raw),
            price_kgs=parse_price(kgs_raw),
            price_period=self._period(deal, usd_raw, kgs_raw),
            price_usd_per_m2=parse_price(
                clean_text(self.text_of(card, Selectors.CARD_PRICE_UNIT))
            ),
            views=to_int(self.text_of(card, Selectors.CARD_VIEWS)),
            favourites=to_int(self.text_of(card, Selectors.CARD_FAVORITES)),
            upped_raw=upped_raw,
            upped_date=self.dates.parse(upped_raw, now),
            posted_raw=posted_raw,
            posted_date=self.dates.parse(posted_raw, now),
            promo=promo,
            is_vip="vip" in promo,
            is_premium="premium" in promo,
            is_top="top" in promo,
            is_urgent="urgent" in promo,
            owner_badge=card.select_one(f"{Selectors.CARD_OWNERSHIP}.owner") is not None,
            title=clean_text(self.text_of(card, Selectors.CARD_TITLE)),
            address=clean_text(self.text_of(card, Selectors.CARD_ADDRESS)),
            complex_slug=self._complex_slug(card),
        )

    # -- pieces ------------------------------------------------------------

    @staticmethod
    def _period(deal: str, usd_raw: str | None, kgs_raw: str | None) -> str:
        """Same rule as the detail parser: the deal decides, the suffix refines.

        The price string alone is not a reliable signal — plenty of rent prices
        render with no suffix — so `deal`, known for free from the crawl stream,
        is authoritative and the suffix only separates daily from monthly.
        """
        if deal != "rent":
            return "total"
        blob = f"{usd_raw or ''} {kgs_raw or ''}".lower()
        return "day" if ("сут" in blob or "ноч" in blob or "посуточ" in blob) else "month"

    def _card_dates(self, card: Tag) -> tuple[str | None, str | None]:
        """The single date span means *bumped* or *posted*, never both.

        The bump icon is what distinguishes them; without it the span is the
        posting date. Reading it as one field would silently merge two different
        events, so they are kept apart.
        """
        span = card.select_one(Selectors.CARD_DATE)
        if span is None:
            return None, None
        text = clean_text(span.get_text(" ", strip=True))
        if not text:
            return None, None
        if span.select_one(Selectors.CARD_BUMP_ICON):
            return None, text
        return text, None

    def _promo(self, card: Tag) -> str:
        """Paid promotion badges as a sorted, comma-joined string ("top,vip").

        Read from the class list rather than a fixed set of columns, so a paid
        tier the site adds later still lands in the data.
        """
        kinds: set[str] = set()
        for node in card.select(Selectors.CARD_PROMO):
            for css_class in self.classes(node):
                if css_class.startswith("is-"):
                    kinds.add(css_class[3:])
        return ",".join(sorted(kinds))

    def _complex_slug(self, card: Tag) -> str | None:
        href = self.attr(card.select_one(Selectors.CARD_COMPLEX), "href") or ""
        if "/jilie-kompleksy/" not in href:
            return None
        return href.split("/jilie-kompleksy/")[-1].strip("/") or None
