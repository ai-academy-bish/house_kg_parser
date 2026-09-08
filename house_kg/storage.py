"""Persistence: append-only JSONL tables + a photo store.

Resumability is the whole point of this module. A full crawl is a multi-hour,
~26 000-listing, ~46 GB job; it *will* be interrupted. So:

* every record is appended to JSONL the moment it is parsed — nothing is held in
  memory until the end, and a kill -9 loses at most the record in flight;
* on start-up each table reports the keys it already holds, and the crawler skips
  them, so a restart resumes instead of re-downloading;
* photos are content-addressed by file existence: a photo already on disk is never
  fetched twice.

Repeat crawls add a second requirement that pulls the opposite way: a listing seen
again must be *re-recorded*, not skipped. Rather than making the tables mutable,
they stay append-only and gain versions:

* `JsonlTable`      — one row per key, later duplicates dropped (photos, reviews).
* `VersionedTable`  — many rows per key, latest wins; a new row is written only
                      when the content actually changed, so the file grows with
                      real edits rather than with run count.
* `SnapshotStore`   — per-snapshot observation and change files, one small file
                      per run, which is also exactly the partitioning the Hub
                      upload wants.

Nothing is ever rewritten in place, so a kill -9 still costs at most one record.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .constants import TRACKED_ENTITY_FIELDS
from .logging_utils import get_logger

logger = get_logger(__name__)


class JsonlTable:
    """An append-only JSONL file with a de-duplicating key index.

    Thread-safe: the crawler writes from a worker pool.
    """

    def __init__(self, path: Path, key: str) -> None:
        self.path = path
        self.key = key
        self._lock = threading.Lock()
        self._keys: set[str] = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load_keys()

    def _load_keys(self) -> None:
        """Index what a previous run already wrote (this is what makes resume work)."""
        if not self.path.exists():
            return
        recovered = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # a partially-written final line from a hard kill: drop it
                    logger.warning("skipping corrupt line in %s", self.path.name)
                    continue
                value = row.get(self.key)
                if value is not None:
                    self._keys.add(str(value))
                    recovered += 1
        if recovered:
            logger.info("resuming %s: %d existing rows", self.path.name, recovered)

    # -- reads -------------------------------------------------------------

    def __contains__(self, key: object) -> bool:
        return str(key) in self._keys

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def keys(self) -> set[str]:
        return set(self._keys)

    def rows(self) -> Iterator[dict[str, Any]]:
        """Stream every row back (used by the dataset builder)."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

    # -- writes ------------------------------------------------------------

    def append(self, row: dict[str, Any]) -> bool:
        """Append unless the key is already present. Returns True if written."""
        value = row.get(self.key)
        if value is None:
            raise ValueError(f"row is missing key field {self.key!r}")
        value = str(value)

        with self._lock:
            if value in self._keys:
                return False
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
            self._keys.add(value)
        return True

    def extend(self, rows: Iterable[dict[str, Any]]) -> int:
        return sum(1 for row in rows if self.append(row))


class PhotoStore:
    """Flat directory of images named with uuid4.

    Flat on purpose: the dataset ships photos as an embedded HF `Image` feature,
    so directory structure carries no meaning — the FK in the `photos` table does.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def new_id(self) -> str:
        return uuid.uuid4().hex

    def path_for(self, foto_id: str, extension: str = ".jpg") -> Path:
        return self.directory / f"{foto_id}{extension}"

    def save(self, data: bytes, url: str) -> tuple[str, Path]:
        """Write bytes under a fresh uuid; returns (foto_id, path)."""
        extension = ".jpg"
        for candidate in (".jpeg", ".png", ".webp", ".jpg"):
            if url.lower().endswith(candidate):
                extension = candidate
                break
        foto_id = self.new_id()
        path = self.path_for(foto_id, extension)
        path.write_bytes(data)
        return foto_id, path

    def existing(self) -> dict[str, str]:
        """foto_id -> file name, for everything already on disk."""
        return {p.stem: p.name for p in self.directory.iterdir() if p.is_file()}

    def __len__(self) -> int:
        return sum(1 for p in self.directory.iterdir() if p.is_file())


class VersionedTable(JsonlTable):
    """Append-only table that may hold several versions of the same key.

    `JsonlTable` refuses a key it already holds — exactly right while one crawl
    resumes, exactly wrong when the point of the run is to re-read an entity that
    may have changed. This subclass appends a new version instead, but *only when
    something tracked actually differs*: an unchanged entity writes nothing, so
    the file grows with real edits rather than with the number of runs.

    Only the tracked fields are held in memory (a few hundred bytes per key, not
    the whole row), so indexing 25k listings costs megabytes, not gigabytes.
    """

    #: Lifecycle columns carried forward from the first version of a key.
    CARRIED = ("first_seen", "first_snapshot")

    def __init__(self, path: Path, key: str, tracked: Iterable[str] = ()) -> None:
        self.tracked = tuple(tracked)
        self._state: dict[str, dict[str, Any]] = {}
        super().__init__(path, key)

    def _load_keys(self) -> None:
        """Index the latest version of every key (this is what makes diffing work)."""
        if not self.path.exists():
            return
        recovered = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("skipping corrupt line in %s", self.path.name)
                    continue
                value = row.get(self.key)
                if value is None:
                    continue
                value = str(value)
                self._keys.add(value)
                self._state[value] = self._compact(row)
                recovered += 1
        if recovered:
            logger.info(
                "resuming %s: %d rows, %d distinct keys",
                self.path.name, recovered, len(self._keys),
            )

    def _compact(self, row: dict[str, Any]) -> dict[str, Any]:
        """Keep only what diffing and lifecycle need."""
        keep = {f: row.get(f) for f in self.tracked}
        for f in self.CARRIED:
            if row.get(f) is not None:
                keep[f] = row[f]
        return keep

    def current(self, key: object) -> dict[str, Any] | None:
        """Tracked state of the latest version, or None if the key is unknown."""
        return self._state.get(str(key))

    def diff(self, row: dict[str, Any]) -> list[tuple[str, Any, Any]]:
        """(field, old, new) for every tracked field that moved. Empty if new."""
        previous = self._state.get(str(row.get(self.key)))
        if previous is None:
            return []
        out: list[tuple[str, Any, Any]] = []
        for f in self.tracked:
            old, new = previous.get(f), row.get(f)
            if old != new:
                out.append((f, old, new))
        return out

    def upsert(self, row: dict[str, Any]) -> tuple[str, list[tuple[str, Any, Any]]]:
        """Write a version if the key is new or a tracked field moved.

        Returns ("new" | "changed" | "unchanged", diffs).
        """
        value = row.get(self.key)
        if value is None:
            raise ValueError(f"row is missing key field {self.key!r}")
        value = str(value)

        with self._lock:
            previous = self._state.get(value)
            if previous is None:
                status, diffs = "new", []
            else:
                diffs = [
                    (f, previous.get(f), row.get(f))
                    for f in self.tracked
                    if previous.get(f) != row.get(f)
                ]
                if not diffs:
                    return "unchanged", []
                status = "changed"
                # a later version must not lose when the entity was first seen
                for f in self.CARRIED:
                    if previous.get(f) is not None:
                        row[f] = previous[f]

            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
            self._keys.add(value)
            self._state[value] = self._compact(row)
        return status, diffs

    def latest(self) -> Iterator[dict[str, Any]]:
        """Fold the file to one row per key, last version wins."""
        folded: dict[str, dict[str, Any]] = {}
        for row in self.rows():
            value = row.get(self.key)
            if value is not None:
                folded[str(value)] = row
        yield from folded.values()

    def versions(self) -> int:
        """Total rows on disk (>= len(self), which counts distinct keys)."""
        return sum(1 for _ in self.rows())


class SnapshotStore:
    """Per-snapshot observation and change files.

    One file per run rather than one growing file, for three reasons: reading the
    *previous* snapshot back (to diff against) touches 25k rows instead of the
    whole history; a partial run is discarded by deleting one file; and the layout
    is already the partitioning the Hub upload needs.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.observations_dir = root / "observations"
        self.changes_dir = root / "changes"
        for d in (self.observations_dir, self.changes_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- per-snapshot tables ----------------------------------------------

    def observations(self, snapshot_id: str) -> JsonlTable:
        """Observation table for one snapshot, keyed for idempotent re-runs."""
        return JsonlTable(self.observations_dir / f"{snapshot_id}.jsonl", key="obs_id")

    def changes(self, snapshot_id: str) -> JsonlTable:
        return JsonlTable(self.changes_dir / f"{snapshot_id}.jsonl", key="change_id")

    # -- history -----------------------------------------------------------

    def snapshot_ids(self) -> list[str]:
        return sorted(p.stem for p in self.observations_dir.glob("*.jsonl"))

    def previous_of(self, snapshot_id: str) -> str | None:
        """The most recent snapshot before this one, if any."""
        earlier = [s for s in self.snapshot_ids() if s < snapshot_id]
        return earlier[-1] if earlier else None

    def load_observations(self, snapshot_id: str | None) -> dict[str, dict[str, Any]]:
        """house_kg_id -> observation row, for one snapshot. Empty if none."""
        if not snapshot_id:
            return {}
        path = self.observations_dir / f"{snapshot_id}.jsonl"
        if not path.exists():
            return {}
        out: dict[str, dict[str, Any]] = {}
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = row.get("house_kg_id")
                if key:
                    out[str(key)] = row
        return out

    def active_listings(self, snapshot_id: str | None) -> set[str]:
        """Listings that were live as of the given snapshot."""
        return {
            k for k, row in self.load_observations(snapshot_id).items()
            if row.get("is_active", True)
        }


#: Listing fields whose change is worth a new version row. Deliberately excludes
#: `views`, `pars_date` and the relative `*_raw`/`*_date` pairs: all of those move
#: on every read without the advertisement having changed at all.
LISTING_CONTENT_FIELDS: tuple[str, ...] = (
    "price_usd",
    "price_kgs",
    "price_period",
    "title",
    "description",
    "address",
    "rooms_n",
    "area_m2",
    "seller_type",
    "offer_type",
    "company_slug",
    "complex_slug",
    "latitude",
    "longitude",
)


class Storage:
    """Every table plus the photo store, wired to the configured paths."""

    def __init__(self, raw_dir: Path, photos_dir: Path) -> None:
        #: Listings, companies, complexes and users are versioned: a re-read that
        #: differs appends a new version instead of being silently dropped.
        self.listings = VersionedTable(
            raw_dir / "listings.jsonl", key="house_kg_id", tracked=LISTING_CONTENT_FIELDS
        )
        self.users = VersionedTable(
            raw_dir / "users.jsonl", key="user_id", tracked=("name", "ads_count")
        )
        self.companies = VersionedTable(
            raw_dir / "companies.jsonl", key="slug", tracked=TRACKED_ENTITY_FIELDS
        )
        self.complexes = VersionedTable(
            raw_dir / "complexes.jsonl", key="slug", tracked=TRACKED_ENTITY_FIELDS
        )
        #: Reviews and photos are immutable once seen — plain de-duplication.
        self.reviews = JsonlTable(raw_dir / "reviews.jsonl", key="review_id")
        self.photos = JsonlTable(raw_dir / "photos.jsonl", key="foto_id")
        self.snapshots = VersionedTable(
            raw_dir / "snapshots.jsonl", key="snapshot_id", tracked=("complete", "finished_at")
        )
        self.snapshot_store = SnapshotStore(raw_dir)
        self.photo_store = PhotoStore(photos_dir)

    def summary(self) -> dict[str, int]:
        return {
            "listings": len(self.listings),
            "users": len(self.users),
            "companies": len(self.companies),
            "complexes": len(self.complexes),
            "reviews": len(self.reviews),
            "photos": len(self.photos),
        }
