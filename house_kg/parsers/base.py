"""Parser base class."""

from __future__ import annotations

from datetime import datetime, timezone

from bs4 import BeautifulSoup

from ..logging_utils import get_logger
from ..utils import RussianDateParser, Transliterator


class BaseParser:
    """Shared machinery for every page parser.

    Holds the collaborators (date parser, transliterator) so subclasses can swap
    them out — e.g. a Kyrgyz-language variant of the site would only need a
    different `Transliterator`, not a new parser.
    """

    def __init__(
        self,
        dates: RussianDateParser | None = None,
        translit: Transliterator | None = None,
    ) -> None:
        self.dates = dates or RussianDateParser()
        self.translit = translit or Transliterator()
        self.logger = get_logger(type(self).__module__)

    @staticmethod
    def soup(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def text_of(node: object, selector: str) -> str | None:
        """Text of the first match, or None."""
        el = node.select_one(selector)  # type: ignore[attr-defined]
        return el.get_text(" ", strip=True) if el else None

    @staticmethod
    def attr(node: object, name: str) -> str | None:
        """One attribute as a plain string.

        bs4 types multi-valued attributes (`class`, `rel`) as a list, so every
        caller would otherwise have to narrow the union by hand.
        """
        if node is None:
            return None
        value = node.get(name)  # type: ignore[attr-defined]
        if isinstance(value, list):
            return " ".join(value) or None
        return value or None

    @staticmethod
    def classes(node: object) -> list[str]:
        """The element's CSS classes, always as a list."""
        value = node.get("class")  # type: ignore[attr-defined]
        if value is None:
            return []
        return list(value) if isinstance(value, list) else str(value).split()
