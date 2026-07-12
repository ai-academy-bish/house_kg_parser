"""Persistence: append-only JSONL tables + a photo store.

Resumability is the whole point of this module. A full crawl is a multi-hour,
~26 000-listing, ~46 GB job; it *will* be interrupted. So:

* every record is appended to JSONL the moment it is parsed — nothing is held in
  memory until the end, and a kill -9 loses at most the record in flight;
* on start-up each table reports the keys it already holds, and the crawler skips
  them, so a restart resumes instead of re-downloading;
* photos are content-addressed by file existence: a photo already on disk is never
  fetched twice.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

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


class Storage:
    """The five tables plus the photo store, wired to the configured paths."""

    def __init__(self, raw_dir: Path, photos_dir: Path) -> None:
        self.listings = JsonlTable(raw_dir / "listings.jsonl", key="house_kg_id")
        self.users = JsonlTable(raw_dir / "users.jsonl", key="user_id")
        self.companies = JsonlTable(raw_dir / "companies.jsonl", key="slug")
        self.complexes = JsonlTable(raw_dir / "complexes.jsonl", key="slug")
        self.reviews = JsonlTable(raw_dir / "reviews.jsonl", key="review_id")
        self.photos = JsonlTable(raw_dir / "photos.jsonl", key="foto_id")
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
