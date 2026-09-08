"""Build the HuggingFace dataset — a fixed set of subsets, a growing set of files.

The layout is the whole point of the incremental design:

    data/listings.parquet              latest state, rewritten each run (~23 MB)
    data/companies.parquet             latest state, rewritten
    data/complexes.parquet             latest state, rewritten
    data/users.parquet                 latest state, rewritten
    data/reviews.parquet               everything ever seen, rewritten
    data/snapshots.parquet             one row per run, rewritten
    data/observations/obs-<id>.parquet APPEND — one small file per run
    data/changes/chg-<id>.parquet      APPEND — one small file per run
    data/photos/photos-<id>-N.parquet  APPEND — only the newly fetched images

Subsets are *configs*, files inside them are *partitions*. A dated config per run
(`listings_2026_09_08`) was the obvious alternative and is a trap: it would give a
year's worth of runs 50-odd configs, force `load_dataset` to be called once per
week and concatenated by hand, and make the dataset viewer useless. One config
holding many files reads back as a single table with a `snapshot_id` column,
which is what a panel actually wants.

Nothing already on the Hub is rebuilt or re-uploaded. What has been published is
read back from the Hub itself rather than from local bookkeeping, so a machine
that lost its disk still appends correctly after `house-kg pull`.
"""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from datasets import Dataset, Features, Image, Value
from huggingface_hub import HfApi
from huggingface_hub.utils import HfHubHTTPError

from ..config import Config
from ..logging_utils import ProgressTracker, get_logger
from ..models import Change, Observation
from ..storage import Storage
from .card import build_card
from .schema import features_for

logger = get_logger(__name__)

#: subset name -> the Storage attribute it is built from
_TABLE_FOR = {"photo_index": "photos"}

#: Subsets rebuilt in full on every run. They are small, and rewriting them is
#: what keeps "current state" actually current.
STATE_SUBSETS = (
    "listings", "users", "companies", "complexes", "reviews",
    "snapshots", "photo_index",
)

#: Subsets that only ever gain files.
PARTITIONED_SUBSETS = ("listing_observations", "changes", "photos")

#: Explicit schemas for the appended subsets. Inferring them per file is not safe:
#: a column that happens to be entirely null in one week's partition is typed
#: `null` by Arrow and then refuses to concatenate with the next week's strings,
#: which breaks `load_dataset` for everyone — and only from the second run on.
PARTITION_FEATURES = {
    "observations": features_for(Observation),
    "changes": features_for(Change),
}


class HFDatasetBuilder:
    """Converts the JSONL tables + photo files into a HF dataset."""

    def __init__(self, config: Config, storage: Storage, progress: ProgressTracker) -> None:
        self.config = config
        self.storage = storage
        self.progress = progress
        self.out_dir = config.dataset_dir
        self.hub = config.dataset.hub
        self._repo_files: list[str] | None = None

    # -- public API --------------------------------------------------------

    def build(self) -> dict[str, int]:
        """Write every subset to `dataset.output_dir`, then optionally push."""
        first_run = self.config.dataset.is_first_run
        published_obs = set() if first_run else self.published_snapshots("observations")
        published_chg = set() if first_run else self.published_snapshots("changes")
        published_photos = set() if first_run else self.published_snapshots("photos")

        # The output directory is a staging area, never the source of truth: it is
        # cleared so a push uploads exactly what this run produced and nothing stale.
        if self.out_dir.exists():
            shutil.rmtree(self.out_dir)
        for sub in ("", "observations", "changes", "photos"):
            (self.out_dir / "data" / sub).mkdir(parents=True, exist_ok=True)

        counts: dict[str, int] = {}
        lifecycle = self._lifecycle()
        for name in STATE_SUBSETS:
            counts[name] = self._build_state_table(name, lifecycle)

        counts["listing_observations"] = self._build_partitions(
            "observations", "obs", published_obs
        )
        counts["changes"] = self._build_partitions("changes", "chg", published_chg)

        if self.config.dataset.include_photos:
            counts["photos"] = self._build_photos(published_photos)

        self._write_card(counts, first_run)

        if self.hub.push:
            self._push(counts, first_run)

        logger.info("[bold green]dataset ready[/] -> %s", self.out_dir)
        return counts

    def published_snapshots(self, kind: str) -> set[str]:
        """Snapshot ids already on the Hub, read from the repository itself.

        Asking the Hub rather than trusting a local marker file is what makes this
        safe after a rebuilt machine: the repository is the record of what exists.
        """
        files = self._list_repo_files()
        prefix = f"data/{kind}/"
        out: set[str] = set()
        for path in files:
            if not path.startswith(prefix) or not path.endswith(".parquet"):
                continue
            stem = Path(path).stem            # obs-2026-09-08 | photos-2026-09-08-00001
            parts = stem.split("-", 1)
            if len(parts) == 2:
                out.add(parts[1].rsplit("-", 1)[0] if kind == "photos" else parts[1])
        if out:
            logger.info("%s already on the Hub: %d snapshot(s)", kind, len(out))
        return out

    def _list_repo_files(self) -> list[str]:
        """Repository file list, fetched once per build."""
        if self._repo_files is not None:
            return self._repo_files
        if not self.hub.repo_id:
            self._repo_files = []
            return self._repo_files
        try:
            self._repo_files = HfApi(token=self.hub.token).list_repo_files(
                repo_id=self.hub.repo_id, repo_type="dataset"
            )
        except (HfHubHTTPError, OSError) as exc:  # no repo yet, or no network
            logger.info("could not list %s (%s) — treating as empty", self.hub.repo_id, exc)
            self._repo_files = []
        return self._repo_files

    # -- current-state subsets --------------------------------------------

    def _build_state_table(self, name: str, lifecycle: dict[str, dict[str, Any]]) -> int:
        """Fold a versioned table to its latest row per key and write one Parquet."""
        table = getattr(self.storage, _TABLE_FOR.get(name, name))
        rows = list(table.latest() if hasattr(table, "latest") else table.rows())
        if name == "photo_index":
            # the image bytes live in the `photos` subset; this is the join key
            # table, small enough to download when restoring a rebuilt machine
            rows = [
                {k: r.get(k) for k in
                 ("foto_id", "listing_id", "house_kg_id", "file_name", "url", "snapshot_id")}
                for r in rows
            ]
        if not rows:
            logger.warning("subset %s is empty — skipped", name)
            return 0

        if name == "listings":
            for row in rows:
                row.update(lifecycle.get(str(row.get("house_kg_id")), {}))

        rows = self._align(rows)
        dataset = Dataset.from_list(rows)
        target = self.out_dir / "data" / f"{name}.parquet"
        dataset.to_parquet(target)

        logger.info(
            "  %-20s %6d rows  %3d cols  %6.2f MB",
            name, dataset.num_rows, len(dataset.column_names),
            target.stat().st_size / 1e6,
        )
        return dataset.num_rows

    def _lifecycle(self) -> dict[str, dict[str, Any]]:
        """Derive each listing's lifecycle from the observation history.

        Kept out of `listings.jsonl` deliberately: writing `last_seen` back on
        every run would append 25 000 rows a week to a file that should only grow
        when an advertisement is actually edited. The panel already holds the
        answer, so the answer is computed from the panel.
        """
        store = self.storage.snapshot_store
        snapshots = store.snapshot_ids()
        if not snapshots:
            return {}
        newest = snapshots[-1]

        out: dict[str, dict[str, Any]] = {}
        for snapshot_id in snapshots:
            for key, row in store.load_observations(snapshot_id).items():
                entry = out.setdefault(
                    key,
                    {"first_observed_snapshot": snapshot_id, "observations_n": 0,
                     "delisted_snapshot": None},
                )
                entry["observations_n"] += 1
                if row.get("is_active", True):
                    entry["last_snapshot"] = snapshot_id
                    entry["last_seen"] = row.get("observed_at")
                    entry["delisted_snapshot"] = None
                else:
                    entry["delisted_snapshot"] = snapshot_id

        for entry in out.values():
            entry["is_active"] = (
                entry.get("last_snapshot") == newest and entry["delisted_snapshot"] is None
            )
        return out

    # -- partitioned subsets ----------------------------------------------

    def _build_partitions(self, kind: str, prefix: str, published: set[str]) -> int:
        """One Parquet per snapshot, skipping whatever the Hub already holds."""
        store = self.storage.snapshot_store
        source_dir = store.observations_dir if kind == "observations" else store.changes_dir
        written = 0
        for path in sorted(source_dir.glob("*.jsonl")):
            snapshot_id = path.stem
            if snapshot_id in published:
                continue
            rows = list(_read_jsonl(path))
            if not rows:
                continue
            target = self.out_dir / "data" / kind / f"{prefix}-{snapshot_id}.parquet"
            features = PARTITION_FEATURES[kind]
            dataset = Dataset.from_list(
                [{name: row.get(name) for name in features} for row in rows],
                features=features,
            )
            dataset.to_parquet(target)
            written += dataset.num_rows
            logger.info(
                "  %-20s %6d rows  -> %s", f"{kind}/{snapshot_id}", dataset.num_rows, target.name
            )
        if not written:
            logger.info("  %-20s nothing new to publish", kind)
        return written

    def _build_photos(self, published: set[str]) -> int:
        """Image subset: only images fetched by snapshots not yet on the Hub."""
        photo_dir = self.config.paths.photos
        present = {p.name for p in photo_dir.iterdir() if p.is_file()}

        by_snapshot: dict[str, list[dict[str, Any]]] = defaultdict(list)
        missing = 0
        for row in self.storage.photos.rows():
            snapshot_id = str(row.get("snapshot_id") or "unknown")
            if snapshot_id in published:
                continue
            if row.get("file_name") not in present:
                missing += 1
                continue
            by_snapshot[snapshot_id].append(row)

        if missing:
            logger.warning("%d photo rows have no file on disk — excluded", missing)
        if not by_snapshot:
            logger.info("  %-20s nothing new to publish", "photos")
            return 0

        features = Features(
            {
                "foto_id": Value("string"),
                "listing_id": Value("string"),
                "house_kg_id": Value("string"),
                "url": Value("string"),
                "snapshot_id": Value("string"),
                "image": Image(),
            }
        )
        limit = _parse_size(self.config.dataset.max_shard_size)
        out_dir = self.out_dir / "data" / "photos"
        total = sum(len(v) for v in by_snapshot.values())
        self.progress.track("dataset", total, "packing photos")

        written = 0
        for snapshot_id, rows in sorted(by_snapshot.items()):
            written += self._write_photo_shards(
                rows, snapshot_id, out_dir, features, limit, photo_dir
            )
        self.progress.complete("dataset")

        size_mb = sum(p.stat().st_size for p in out_dir.glob("*.parquet")) / 1e6
        logger.info("  %-20s %6d rows  %6.1f MB", "photos", written, size_mb)
        return written

    def _write_photo_shards(
        self,
        rows: list[dict[str, Any]],
        snapshot_id: str,
        out_dir: Path,
        features: Features,
        limit: int,
        photo_dir: Path,
    ) -> int:
        """Stream one snapshot's images into byte-sized shards.

        Each shard is written and released as soon as it is full, so peak memory
        stays at one shard no matter how large the photo set grows.
        """
        batch: list[dict[str, Any]] = []
        batch_bytes = 0
        index = 0
        written = 0

        def flush() -> None:
            nonlocal batch, batch_bytes, index, written
            if not batch:
                return
            path = out_dir / f"photos-{snapshot_id}-{index:05d}.parquet"
            Dataset.from_list(batch, features=features).to_parquet(path)
            written += len(batch)
            index += 1
            batch, batch_bytes = [], 0

        for row in rows:
            data = (photo_dir / row["file_name"]).read_bytes()
            batch.append(
                {
                    "foto_id": row["foto_id"],
                    "listing_id": row["listing_id"],
                    "house_kg_id": row["house_kg_id"],
                    "url": row.get("url"),
                    "snapshot_id": snapshot_id,
                    # bytes are embedded, so each shard is self-contained
                    "image": {"path": row["file_name"], "bytes": data},
                }
            )
            batch_bytes += len(data)
            self.progress.advance("dataset")
            if batch_bytes >= limit:
                flush()
        flush()
        return written

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

    # -- card & hub --------------------------------------------------------

    def _write_card(self, counts: dict[str, int], first_run: bool) -> None:
        snapshots = self.storage.snapshot_store.snapshot_ids()
        (self.out_dir / "README.md").write_text(
            build_card(counts, snapshots, first_run=first_run), encoding="utf-8"
        )
        docs = self.config.project_root / "docs" / "house_kg_dataset.md"
        if docs.exists():
            shutil.copy(docs, self.out_dir / "DATASET_GUIDE.md")

    def _push(self, counts: dict[str, int], first_run: bool) -> None:
        """Upload `hf_dataset/` to the Hub verbatim.

        We deliberately do NOT use `Dataset.push_to_hub` per config. That method
        invents its own file layout (`<config>/train/0000.parquet`) *and* rewrites
        the repo README with configs pointing at it — which then disagrees with the
        card we wrote (`data/<config>.parquet`). The result is a repo whose README
        advertises files that do not exist, and a viewer that 404s.

        `delete_patterns` is never set: everything already in the repository is
        exactly what this run is appending to.
        """
        if not self.hub.repo_id:
            raise ValueError("dataset.hub.push is true but hub.repo_id is not set")

        api = HfApi(token=self.hub.token)
        api.create_repo(
            repo_id=self.hub.repo_id,
            repo_type="dataset",
            private=self.hub.private,
            exist_ok=True,
        )

        files = [f for f in self.out_dir.rglob("*") if f.is_file()]
        total_mb = sum(f.stat().st_size for f in files) / 1e6
        logger.info(
            "pushing %d files (%.0f MB) to https://huggingface.co/datasets/%s",
            len(files), total_mb, self.hub.repo_id,
        )

        snapshots = self.storage.snapshot_store.snapshot_ids()
        latest = snapshots[-1] if snapshots else "initial"
        message = (
            f"Baseline snapshot {latest} ({counts.get('listings', 0)} listings)"
            if first_run
            else f"Snapshot {latest}: +{counts.get('listing_observations', 0)} observations, "
            f"{counts.get('changes', 0)} changes"
        )
        api.upload_folder(
            folder_path=str(self.out_dir),
            repo_id=self.hub.repo_id,
            repo_type="dataset",
            commit_message=message,
        )
        logger.info(
            "[bold green]pushed[/] -> https://huggingface.co/datasets/%s", self.hub.repo_id
        )


def _read_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _parse_size(text: str) -> int:
    """'500MB' -> bytes."""
    units = {"KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}
    value = text.strip().upper()
    for suffix, factor in units.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * factor)
    return int(value)
