"""Restore local crawl state from a published dataset.

Normally this is never needed: `data/raw/*.jsonl` is the source of truth and the
Hub is only the publication target. It becomes essential the moment the machine
that holds that state is rebuilt — a Vast.ai instance without a mounted volume
loses `/workspace` on recycle, and a scheduled crawler that lost its state would
otherwise re-download the entire board and republish it as brand new.

Only the tabular subsets are pulled — a few tens of MB. The `photos` subset holds
the embedded images (tens of GB) and is never fetched: `photo_index` carries the
same rows without the bytes, which is all the crawler needs to know that a
listing's images were already collected.
"""

from __future__ import annotations

import json
from pathlib import Path

from huggingface_hub import snapshot_download
from huggingface_hub.utils import HfHubHTTPError

from ..config import Config
from ..logging_utils import get_logger

logger = get_logger(__name__)

#: repo path -> local JSONL file under data/raw/
FLAT_TABLES: dict[str, str] = {
    "data/listings.parquet": "listings.jsonl",
    "data/users.parquet": "users.jsonl",
    "data/companies.parquet": "companies.jsonl",
    "data/complexes.parquet": "complexes.jsonl",
    "data/reviews.parquet": "reviews.jsonl",
    "data/snapshots.parquet": "snapshots.jsonl",
    "data/photo_index.parquet": "photos.jsonl",
}

#: Columns `HFDatasetBuilder._lifecycle` derives; they are recomputed on the next
#: build and must not be written back into the crawler's source of truth.
DERIVED_LISTING_COLUMNS: tuple[str, ...] = (
    "last_seen",
    "last_snapshot",
    "is_active",
    "observations_n",
    "first_observed_snapshot",
    "delisted_snapshot",
)

#: Everything small enough to restore. The `photos` subset is deliberately absent.
ALLOW_PATTERNS = [
    "data/*.parquet",
    "data/observations/*.parquet",
    "data/changes/*.parquet",
]


class Bootstrapper:
    """Rebuilds `data/raw/` from the Hub so a fresh machine can keep appending."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.hub = config.dataset.hub
        self.raw = config.paths.raw

    def run(self, force: bool = False) -> dict[str, int]:
        if not self.hub.repo_id:
            raise ValueError("dataset.hub.repo_id is not set — nothing to pull from")

        existing = list(self.raw.glob("*.jsonl"))
        if existing and not force:
            logger.warning(
                "local state already present (%d tables in %s) — refusing to overwrite. "
                "Re-run with --force to replace it.",
                len(existing), self.raw,
            )
            return {}

        logger.info("restoring state from https://huggingface.co/datasets/%s", self.hub.repo_id)
        try:
            local = snapshot_download(
                repo_id=self.hub.repo_id,
                repo_type="dataset",
                allow_patterns=ALLOW_PATTERNS,
                token=self.hub.token,
            )
        except HfHubHTTPError as exc:
            logger.error("could not download %s: %s", self.hub.repo_id, exc)
            raise

        root = Path(local)
        counts: dict[str, int] = {}
        for repo_path, filename in FLAT_TABLES.items():
            source = root / repo_path
            if source.exists():
                # only `listings` carries builder-derived lifecycle columns; the
                # observations below own `is_active` outright and must keep it
                strip = DERIVED_LISTING_COLUMNS if filename == "listings.jsonl" else ()
                counts[filename] = _parquet_to_jsonl(source, self.raw / filename, strip)

        for kind, prefix in (("observations", "obs-"), ("changes", "chg-")):
            target_dir = self.raw / kind
            target_dir.mkdir(parents=True, exist_ok=True)
            for source in sorted((root / "data" / kind).glob("*.parquet")):
                snapshot_id = source.stem[len(prefix):]
                counts[f"{kind}/{snapshot_id}"] = _parquet_to_jsonl(
                    source, target_dir / f"{snapshot_id}.jsonl"
                )

        total = sum(counts.values())
        logger.info(
            "[bold green]state restored[/]: %d rows across %d files", total, len(counts)
        )
        logger.info(
            "photos were NOT downloaded — %s already holds them, and `photo_index` "
            "tells the crawler which listings not to re-fetch",
            self.hub.repo_id,
        )
        return counts


def _parquet_to_jsonl(
    source: Path, target: Path, strip: tuple[str, ...] = ()
) -> int:
    """Write a Parquet table back out as the JSONL the crawler reads."""
    import pyarrow.parquet as pq

    table = pq.read_table(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("w", encoding="utf-8") as fh:
        for batch in table.to_batches(max_chunksize=10_000):
            for row in batch.to_pylist():
                for column in strip:
                    row.pop(column, None)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
    logger.info("  %-28s %7d rows", target.name, written)
    return written
