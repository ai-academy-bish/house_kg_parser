"""Company / residential-complex parsers (ratings and reviews)."""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup, Tag

from ..constants import BASE_URL, Selectors
from ..models import Entity, Rating, Review
from ..utils import clean_text, to_int
from .base import BaseParser


class RatingParser(BaseParser):
    """Reads one rating block (`.modal-body`) — score, histogram, reviews."""

    def parse_block(
        self, body: Tag | None, subject_type: str, subject_slug: str, now: datetime
    ) -> tuple[Rating, list[Review]]:
        rating = Rating()
        reviews: list[Review] = []
        if body is None:
            return rating, reviews

        score = body.select_one(Selectors.RATING_SCORE)
        if score:
            try:
                rating.score = float(score.get_text(strip=True).replace(",", "."))
            except ValueError:
                rating.score = None

        rating.count = to_int(self.text_of(body, Selectors.RATING_COUNT))

        # star histogram: 5 rows, the top row is 5 stars, the bottom is 1
        rows = body.select(Selectors.RATING_BARS)
        for index, row in enumerate(rows):
            stars = str(5 - index)
            rating.distribution[stars] = to_int(self.text_of(row, "span")) or 0

        for item in body.select(Selectors.REVIEW_ITEM):
            reviews.append(self._review(item, subject_type, subject_slug, now))

        rating.scraped = len(reviews)
        return rating, reviews

    def _review(self, item: Tag, subject_type: str, subject_slug: str, now: datetime) -> Review:
        link = item.select_one(Selectors.REVIEW_AUTHOR_LINK)
        user_id = None
        if isinstance(link, Tag):
            href = link.get("href", "")
            if href.startswith("/user/"):
                user_id = href.split("/user/")[-1].strip("/")

        date_raw = clean_text(self.text_of(item, Selectors.REVIEW_DATE))
        return Review(
            subject_type=subject_type,
            subject_slug=subject_slug,
            user_id=user_id,
            author=clean_text(self.text_of(item, Selectors.REVIEW_AUTHOR)),
            rating=len(item.select(Selectors.REVIEW_STARS)) or None,
            text=clean_text(self.text_of(item, Selectors.REVIEW_TEXT)),
            date_raw=date_raw,
            date=self.dates.parse(date_raw, now),
        )


class ListingRatingsParser(RatingParser):
    """Splits the two rating blocks a *listing* page can carry.

    The modal may hold TWO independent ratings, and conflating them would be wrong
    (a bad agency is not a bad building):

        .modal-body        -> the agency / company   ("рейтинг компании")
        .modal-body.alt    -> the residential complex ("рейтинг жилого комплекса")

    Either, both, or neither may be present. This parser exists mainly for
    completeness — the pipeline reads ratings from the entity profiles instead,
    so that a company shared by 500 listings is fetched once, not 500 times.
    """

    def parse(self, html: str, company_slug: str | None, complex_slug: str | None):
        soup = BeautifulSoup(html, "lxml")
        now = self.now()
        modal = soup.select_one(Selectors.REVIEWS_MODAL)
        bodies = modal.select(Selectors.MODAL_BODY) if modal else []

        company_body = next((b for b in bodies if "alt" not in (b.get("class") or [])), None)
        complex_body = next((b for b in bodies if "alt" in (b.get("class") or [])), None)

        company = self.parse_block(company_body, "company", company_slug or "", now)
        complex_ = self.parse_block(complex_body, "complex", complex_slug or "", now)
        return company, complex_


class EntityParser(RatingParser):
    """Turns a company or complex profile page into an `Entity` + its reviews.

    NOTE on completeness: the profile renders at most 20 reviews and offers no way
    to load more (its `?page=` pagination belongs to the company's *listings*, and
    returns the same 20 reviews; the JS bundle exposes only add/edit/delete). A
    38-review agency therefore yields 20 — a limit of the source, not of this code.
    `Rating.count` vs `Rating.scraped` keeps that visible in the data.
    """

    KINDS = {
        "company": "{base}/{slug}",
        "complex": "{base}/jilie-kompleksy/{slug}",
    }

    def url_for(self, kind: str, slug: str) -> str:
        return self.KINDS[kind].format(base=BASE_URL, slug=slug)

    def parse(self, html: str, kind: str, slug: str) -> tuple[Entity, list[Review]]:
        soup = BeautifulSoup(html, "lxml")
        now = self.now()

        # a profile page carries a single rating block: its own
        body = soup.select_one(f"{Selectors.REVIEWS_MODAL} {Selectors.MODAL_BODY}") or soup
        rating, reviews = self.parse_block(body, kind, slug, now)

        name = clean_text(self.text_of(soup, Selectors.TITLE))
        entity = Entity(
            slug=slug,
            kind=kind,
            name=self._short_name(name),
            url=self.url_for(kind, slug),
            pars_date=now.isoformat(),
            rating=rating,
            reviews=reviews,
        )
        return entity, reviews

    @staticmethod
    def _short_name(name: str | None) -> str | None:
        """Company <h1> runs on into certification blurb; keep the leading name."""
        if not name:
            return None
        return re.split(r"\s+Компания подтвердила", name)[0].strip()[:200] or None
