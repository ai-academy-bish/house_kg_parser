"""Build the HuggingFace dataset — one subset (config) per table.

Photos are embedded as a HF `Image` feature and written as sharded Parquet rather
than shipped as ~230 000 loose files. A repository of that many small files is
painfully slow to clone and to load; embedded-and-sharded is the standard path for
large image datasets and lets students do:

    load_dataset("<repo>", "photos", split="train")

and get decoded PIL images straight away.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from datasets import Dataset, Features, Image, Value
from huggingface_hub import HfApi

from ..config import Config
from ..logging_utils import ProgressTracker, get_logger
from ..storage import Storage
from .card import build_card

logger = get_logger(__name__)

#: Table subsets, in the order they appear on the dataset card.
TABLE_SUBSETS = ("listings", "users", "companies", "complexes", "reviews")


class HFDatasetBuilder:
    """Converts the JSONL tables + photo files into a HF dataset."""

    def __init__(self, config: Config, storage: Storage, progress: ProgressTracker) -> None:
        self.config = config
        self.storage = storage
        self.progress = progress
        self.out_dir = config.dataset_dir

    # -- public API --------------------------------------------------------

    def build(self) -> dict[str, int]:
        """Write every subset to `dataset.output_dir`, then optionally push."""
        if self.out_dir.exists():
            shutil.rmtree(self.out_dir)
        (self.out_dir / "data").mkdir(parents=True, exist_ok=True)

        counts: dict[str, int] = {}
        for name in TABLE_SUBSETS:
            counts[name] = self._build_table(name)

        if self.config.dataset.include_photos:
            counts["photos"] = self._build_photos()

        self._write_card(counts)

        if self.config.dataset.hub.push:
            self._push(counts)

        logger.info("[bold green]dataset ready[/] -> %s", self.out_dir)
        return counts

    # -- subsets -----------------------------------------------------------

    def _table(self, name: str):
        return getattr(self.storage, name)

    def _build_table(self, name: str) -> int:
        """Plain tabular subset: JSONL -> a single Parquet file."""
        rows = list(self._table(name).rows())
        if not rows:
            logger.warning("subset %s is empty — skipped", name)
            return 0

        rows = self._align(rows)
        dataset = Dataset.from_list(rows)
        target = self.out_dir / "data" / f"{name}.parquet"
        dataset.to_parquet(target)

        size_mb = target.stat().st_size / 1e6
        logger.info(
            "  %-10s %6d rows  %3d cols  %6.2f MB",
            name, dataset.num_rows, len(dataset.column_names), size_mb,
        )
        return dataset.num_rows

    @staticmethod
    def _align(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Give every row the same keys.

        Listing characteristics are sparse (a land plot has no `floor`), and Arrow
        needs a single schema, so missing keys are filled with None rather than
        left absent.
        """
        columns: dict[str, None] = {}
        for row in rows:
            for key in row:
                columns.setdefault(key, None)
        return [{key: row.get(key) for key in columns} for row in rows]

    def _build_photos(self) -> int:
        """Image subset: each photo embedded as bytes, sharded by `max_shard_size`."""
        photo_rows = list(self.storage.photos.rows())
        if not photo_rows:
            logger.warning("no photos to package")
            return 0

        photo_dir = self.config.paths.photos
        present = {p.name for p in photo_dir.iterdir() if p.is_file()}
        rows = [r for r in photo_rows if r.get("file_name") in present]
        missing = len(photo_rows) - len(rows)
        if missing:
            logger.warning("%d photo rows have no file on disk — excluded", missing)

        features = Features(
            {
                "foto_id": Value("string"),
                "listing_id": Value("string"),
                "house_kg_id": Value("string"),
                "url": Value("string"),
                "image": Image(),
            }
        )

        # Stream into byte-sized shards rather than materialising one huge table:
        # a full crawl is ~230k images / ~46 GB, which will not fit in memory.
        limit = _parse_size(self.config.dataset.max_shard_size)
        out_dir = self.out_dir / "data"
        self.progress.track("dataset", len(rows), "packing photos")

        # Each shard is written and released as soon as it is full, so peak memory
        # stays at one shard (~500 MB) no matter how large the photo set grows.
        staged: list[Path] = []
        batch: list[dict[str, Any]] = []
        batch_bytes = 0
        total = 0

        def flush() -> None:
            nonlocal batch, batch_bytes, total
            if not batch:
                return
            path = out_dir / f"photos-part-{len(staged):05d}.parquet"
            Dataset.from_list(batch, features=features).to_parquet(path)
            staged.append(path)
            total += len(batch)
            batch, batch_bytes = [], 0

        for row in rows:
            data = (photo_dir / row["file_name"]).read_bytes()
            batch.append(
                {
                    "foto_id": row["foto_id"],
                    "listing_id": row["listing_id"],
                    "house_kg_id": row["house_kg_id"],
                    "url": row.get("url"),
                    # bytes are embedded, so each shard is self-contained
                    "image": {"path": row["file_name"], "bytes": data},
                }
            )
            batch_bytes += len(data)
            self.progress.advance("dataset")
            if batch_bytes >= limit:
                flush()

        flush()
        self.progress.complete("dataset")

        # rename to the conventional shard-of-N pattern now that N is known
        for index, path in enumerate(staged):
            path.rename(out_dir / f"photos-{index:05d}-of-{len(staged):05d}.parquet")

        total_mb = sum(p.stat().st_size for p in out_dir.glob("photos-*.parquet")) / 1e6
        logger.info(
            "  %-10s %6d rows  %d shard(s)  %6.1f MB",
            "photos", total, len(staged), total_mb,
        )
        return total

    # -- card & hub --------------------------------------------------------

    def _write_card(self, counts: dict[str, int]) -> None:
        subsets = [n for n in (*TABLE_SUBSETS, "photos") if counts.get(n)]
        (self.out_dir / "README.md").write_text(
            build_card(counts, subsets), encoding="utf-8"
        )
        docs = self.config.project_root / "docs" / "house_kg_dataset.md"
        if docs.exists():
            shutil.copy(docs, self.out_dir / "DATASET_GUIDE.md")

    def _push(self, counts: dict[str, int]) -> None:
        """Upload `hf_dataset/` to the Hub verbatim.

        We deliberately do NOT use `Dataset.push_to_hub` per config. That method
        invents its own file layout (`<config>/train/0000.parquet`) *and* rewrites
        the repo README with configs pointing at it — which then disagrees with the
        card we wrote (`data/<config>.parquet`). The result is a repo whose README
        advertises files that do not exist, and a viewer that 404s with
        "Object at location photos/train/0000.parquet not found".

        The folder we built is already a complete, self-consistent dataset repo:
        the parquet files and the README's `configs:` paths refer to each other.
        Uploading it as-is keeps them in sync, preserves the dataset card and the
        guide, and avoids re-reading and re-embedding ~46 GB of images.
        """
        hub = self.config.dataset.hub
        if not hub.repo_id:
            raise ValueError("dataset.hub.push is true but hub.repo_id is not set")

        api = HfApi(token=hub.token)
        api.create_repo(
            repo_id=hub.repo_id,
            repo_type="dataset",
            private=hub.private,
            exist_ok=True,
        )

        files = sorted(self.out_dir.rglob("*"))
        total_mb = sum(f.stat().st_size for f in files if f.is_file()) / 1e6
        logger.info(
            "pushing %d subsets (%.0f MB) to https://huggingface.co/datasets/%s",
            len([n for n in counts if counts[n]]), total_mb, hub.repo_id,
        )

        api.upload_folder(
            folder_path=str(self.out_dir),
            repo_id=hub.repo_id,
            repo_type="dataset",
            commit_message=f"Add house.kg dataset ({counts.get('listings', 0)} listings)",
        )
        logger.info(
            "[bold green]pushed[/] -> https://huggingface.co/datasets/%s", hub.repo_id
        )


def _parse_size(text: str) -> int:
    """'500MB' -> bytes."""
    units = {"KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}
    value = text.strip().upper()
    for suffix, factor in units.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * factor)
    return int(value)
