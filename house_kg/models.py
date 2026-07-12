"""Dataset records.

Five tables, linked by stable natural keys. Ratings and reviews do NOT belong to
a listing — they belong to the company or the residential complex, which are
shared by hundreds of listings. Storing them per-listing would duplicate them
thousands of times over a full crawl, so they live in their own tables:

    Listing ──┬── author_user_id → User      (private sellers only)
              ├── company_slug   → Company
              └── complex_slug   → Complex
    Review  ──┬── subject_slug   → Company | Complex   (per subject_type)
              └── user_id        → User
    Photo   ──── listing_id      → Listing

A rating, by contrast, is strictly 1:1 with its entity, so it stays inline on
Company/Complex — a separate `ratings` table would be a join for nothing.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from .constants import REVIEW_CAP


@dataclass(slots=True)
class Record:
    """Base record: knows how to become a plain dict for JSONL/Parquet."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Review(Record):
    """One review of a company or a residential complex.

    house.kg gives reviews no id, so we mint one — but as a *hash*, not a uuid4:
    a fresh uuid on every run would churn the keys and break joins and diffs
    between dataset refreshes. The same review always yields the same id.
    """

    subject_type: str  # "company" | "complex"
    subject_slug: str
    user_id: str | None
    author: str | None
    rating: int | None
    text: str | None
    date_raw: str | None
    date: str | None
    review_id: str = ""

    def __post_init__(self) -> None:
        if not self.review_id:
            self.review_id = self.make_id(
                self.subject_type, self.subject_slug, self.user_id,
                self.date_raw, self.text,
            )

    @staticmethod
    def make_id(
        subject_type: str,
        subject_slug: str | None,
        user_id: str | None,
        date_raw: str | None,
        text: str | None,
    ) -> str:
        key = "|".join(
            [
                subject_type,
                subject_slug or "",
                user_id or "",
                date_raw or "",
                (text or "")[:200],
            ]
        )
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class Rating(Record):
    """An entity's rating: score, totals and the 5-star histogram.

    Inline on the entity (1:1). `count` is what the site *claims*; `scraped` is
    what we could actually read. They differ for two distinct reasons, both of
    which are preserved rather than hidden:

    * the site renders at most `REVIEW_CAP` (20) reviews and exposes no way to
      load more — no working pagination, no ajax endpoint;
    * a user may leave stars without writing any text, so the count includes
      ratings that have no review body at all.
    """

    score: float | None = None
    count: int | None = None
    scraped: int = 0
    distribution: dict[str, int] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        """True only when we actually hit the site's hard cap."""
        return bool(self.count and self.scraped >= REVIEW_CAP and self.count > self.scraped)

    def to_columns(self, prefix: str = "") -> dict[str, Any]:
        """Flatten to columns: a nested dict is awkward in Parquet."""
        cols: dict[str, Any] = {
            f"{prefix}rating": self.score,
            f"{prefix}reviews_count": self.count,
            f"{prefix}reviews_scraped": self.scraped,
            f"{prefix}reviews_truncated": self.truncated,
        }
        for star in ("5", "4", "3", "2", "1"):
            cols[f"{prefix}rating_{star}"] = self.distribution.get(star)
        return cols


@dataclass(slots=True)
class Entity(Record):
    """A company (agency) or a residential complex — the subject of reviews."""

    slug: str
    kind: str  # "company" | "complex"
    name: str | None
    url: str
    pars_date: str
    rating: Rating = field(default_factory=Rating)
    reviews: list[Review] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Row for the companies/complexes table: rating inline, reviews split out."""
        row: dict[str, Any] = {
            "slug": self.slug,
            "kind": self.kind,
            "name": self.name,
            "url": self.url,
            "pars_date": self.pars_date,
        }
        row.update(self.rating.to_columns())
        return row


@dataclass(slots=True)
class User(Record):
    """A person: a listing author, a reviewer, or both.

    Listing authors and reviewers share the same `/user/<hash>` namespace, so the
    table is their UNION — someone who posts ads *and* writes reviews is one row.
    Companies have no owner user: their profile exposes none.
    """

    user_id: str
    url: str
    pars_date: str
    name: str | None = None
    ads_count: int | None = None
    registered_raw: str | None = None
    registered_date: str | None = None
    is_ad_author: bool = False
    is_reviewer: bool = False


@dataclass(slots=True)
class Photo(Record):
    """One downloaded image, linked back to its listing."""

    foto_id: str
    listing_id: str
    house_kg_id: str
    file_name: str
    url: str


@dataclass(slots=True)
class Listing(Record):
    """One advertisement.

    Field names are English; values stay in the original language, exactly as the
    site renders them. Characteristics parsed from `.info-row` are flattened into
    `attributes` and merged into the row on export, so a new site field is never
    dropped.
    """

    id: str
    house_kg_id: str
    source_url: str
    pars_date: str

    deal: str  # sale | rent
    type: str
    region: str
    city: str | None
    address: str | None
    title: str | None
    description: str | None

    latitude: float | None
    longitude: float | None

    price_usd_raw: str | None
    price_kgs_raw: str | None
    price_usd: float | None
    price_kgs: float | None
    price_period: str  # total | month | day

    views: int | None
    posted_raw: str | None
    posted_date: str | None
    upped_raw: str | None
    upped_date: str | None

    seller_type: str  # owner | company
    offer_type: str | None  # what the seller CLAIMS
    declared_owner: bool | None
    seller_mismatch: bool | None

    author_user_id: str | None
    author_name: str | None
    author_url: str | None
    author_ads_count: int | None

    company_slug: str | None
    company_url: str | None
    complex_slug: str | None
    complex_name: str | None
    complex_url: str | None

    rooms_n: int | None
    area_m2: float | None

    foto_ids: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Flatten `attributes` into the row (one column per characteristic)."""
        row = asdict(self)
        attributes = row.pop("attributes")
        for key, value in attributes.items():
            row.setdefault(key, value)  # never clobber a core field
        return row
