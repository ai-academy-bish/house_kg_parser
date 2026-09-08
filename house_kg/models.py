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

Repeated crawls add a time dimension on top of that, split three ways:

    Listing      the *latest known state* of an advertisement, plus its lifecycle
                 (first_seen / last_seen / is_active)
    Observation  one measurement per (snapshot × listing) — the panel data
    Change       one row per field that actually moved — the event log

The split is deliberate. `Observation` answers "what was the price that week"
without replaying anything; `Change` answers "what happened" without scanning a
25k-row-per-week panel. Both are cheap: a snapshot of the whole board is ~2-3 MB.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .constants import REVIEW_CAP

#: Namespace for deterministic listing ids (see `Listing.make_id`).
LISTING_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


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
    """One downloaded image, linked back to its listing.

    `snapshot_id` records the run that fetched it, which is what lets the Hub
    upload ship only the new shards instead of re-embedding ~46 GB every time.
    """

    foto_id: str
    listing_id: str
    house_kg_id: str
    file_name: str
    url: str
    snapshot_id: str = ""


@dataclass(slots=True)
class Listing(Record):
    """One advertisement.

    Field names are English; values stay in the original language, exactly as the
    site renders them. Characteristics parsed from `.info-row` are flattened into
    `attributes` and merged into the row on export, so a new site field is never
    dropped.

    `id` is derived from `house_kg_id`, never random: the same advertisement must
    keep the same id across every snapshot, or `photos.listing_id` breaks the
    moment a listing is re-read on a later run.
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

    #: Set on first sight and never rewritten; `last_seen` moves with each run.
    first_seen: str | None = None
    last_seen: str | None = None
    first_snapshot: str | None = None
    last_snapshot: str | None = None
    is_active: bool = True
    delisted_snapshot: str | None = None

    @staticmethod
    def make_id(house_kg_id: str) -> str:
        """Stable uuid for an advertisement — uuid5, so re-reading reproduces it."""
        return str(uuid.uuid5(LISTING_NAMESPACE, house_kg_id))

    def to_dict(self) -> dict[str, Any]:
        """Flatten `attributes` into the row (one column per characteristic)."""
        row = asdict(self)
        attributes = row.pop("attributes")
        for key, value in attributes.items():
            row.setdefault(key, value)  # never clobber a core field
        return row


@dataclass(slots=True)
class CardObservation(Record):
    """What one result-page card yields — a full observation, no detail fetch.

    The card carries every field that moves between runs (price, views,
    favourites, bump, paid promotion), so a refresh pass costs ~2.6k result pages
    instead of ~25k detail pages plus a photo re-download.
    """

    house_kg_id: str
    source_url: str
    deal: str
    type: str
    region: str

    price_usd: float | None = None
    price_kgs: float | None = None
    price_usd_raw: str | None = None
    price_kgs_raw: str | None = None
    price_period: str = "total"
    price_usd_per_m2: float | None = None

    views: int | None = None
    favourites: int | None = None

    #: The card shows either "поднято N назад" or the posting date — the bump
    #: icon is what distinguishes them, so both are kept apart here.
    upped_raw: str | None = None
    upped_date: str | None = None
    posted_raw: str | None = None
    posted_date: str | None = None

    #: Raw, sorted promotion markers ("top,vip"); "" when the seller paid for none.
    promo: str = ""
    is_vip: bool = False
    is_premium: bool = False
    is_top: bool = False
    is_urgent: bool = False

    #: The card's "Собственник" badge is a *paid* marker, so its absence does not
    #: mean an agency. Recorded as-is; `seller_type` stays a detail-page field.
    owner_badge: bool = False
    title: str | None = None
    address: str | None = None
    complex_slug: str | None = None


@dataclass(slots=True)
class Observation(Record):
    """One (snapshot × listing) measurement — the panel-data fact table.

    Keyed by `obs_id` so re-running the same snapshot is idempotent: an
    interrupted run resumes instead of duplicating rows.
    """

    snapshot_id: str
    house_kg_id: str
    observed_at: str

    price_usd: float | None = None
    price_kgs: float | None = None
    price_period: str = "total"
    price_usd_per_m2: float | None = None
    views: int | None = None
    favourites: int | None = None
    upped_date: str | None = None
    promo: str = ""
    is_vip: bool = False
    is_premium: bool = False
    is_top: bool = False
    is_urgent: bool = False
    owner_badge: bool = False
    is_active: bool = True
    obs_id: str = ""

    def __post_init__(self) -> None:
        if not self.obs_id:
            self.obs_id = f"{self.snapshot_id}|{self.house_kg_id}"


@dataclass(slots=True)
class Change(Record):
    """One recorded difference against the previous snapshot.

    Long format — one row per changed field — so listings, companies, complexes
    and reviews all share a single table and a single query shape.
    """

    snapshot_id: str
    observed_at: str
    entity_type: str  # listing | company | complex | review
    entity_key: str
    change_type: str  # appeared | delisted | field_changed | bumped | ...
    field: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    prev_snapshot_id: str | None = None
    change_id: str = ""

    def __post_init__(self) -> None:
        if not self.change_id:
            key = "|".join(
                [
                    self.snapshot_id,
                    self.entity_type,
                    self.entity_key,
                    self.change_type,
                    self.field or "",
                ]
            )
            self.change_id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class Snapshot(Record):
    """Metadata for one crawl run.

    Without this table a run that died at 60% is indistinguishable from 40% of
    the board being delisted overnight — `complete` is what makes a gap in
    `listing_observations` interpretable.
    """

    snapshot_id: str
    started_at: str
    finished_at: str | None = None
    mode: str = "refresh"  # baseline | refresh
    complete: bool = False
    scope_deals: str = ""
    scope_types: str = ""
    scope_regions: str = ""
    pages_scanned: int = 0
    listings_seen: int = 0
    listings_new: int = 0
    listings_changed: int = 0
    listings_delisted: int = 0
    listings_reappeared: int = 0
    photos_new: int = 0
    entities_changed: int = 0
    reviews_new: int = 0
    duration_seconds: float | None = None
