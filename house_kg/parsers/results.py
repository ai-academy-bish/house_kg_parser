"""Result-page parser: listing URLs and pagination."""

from __future__ import annotations

import re

from ..constants import BASE_URL, Selectors
from .base import BaseParser


class ResultsParser(BaseParser):
    """Reads a `/kupit-*?region=N&page=M` result page.

    Only URLs are taken from result pages — every field is read from the detail
    page, which carries far more (coordinates, photos, characteristics, author).
    """

    def listing_urls(self, html: str) -> list[str]:
        soup = self.soup(html)
        urls: list[str] = []
        for card in soup.select(Selectors.LISTING_CARD):
            link = card.select_one(Selectors.CARD_LINK)
            if link and link.get("href"):
                urls.append(BASE_URL + link["href"].split("?")[0])
        return urls

    def last_page(self, html: str) -> int:
        """Page number behind the «Последняя» link (1 when there is no pagination)."""
        soup = self.soup(html)
        for anchor in soup.select(Selectors.PAGINATION):
            if "Последняя" in anchor.get_text():
                m = re.search(r"page=(\d+)", anchor.get("href", ""))
                if m:
                    return int(m.group(1))
        return 1 if soup.select(Selectors.LISTING_CARD) else 0
