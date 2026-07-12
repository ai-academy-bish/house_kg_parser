"""Typed configuration, loaded from `config.yaml`.

Every knob the pipeline honours lives here as a dataclass, so configuration is
validated and discoverable rather than a dict of strings passed around. Sub-classes
may override any section to specialise a run without touching the YAML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .constants import DEALS, PROPERTY_TYPES, REGIONS


@dataclass(slots=True)
class ScopeConfig:
    """What to crawl: deals, property types, regions.

    Regions default to all of Kyrgyzstan (ids 1-7). Users may narrow the scope,
    but ids outside 1-7 are other countries and are rejected outright.
    """

    deals: list[str] = field(default_factory=lambda: list(DEALS))
    property_types: list[str] = field(default_factory=lambda: list(PROPERTY_TYPES))
    regions: list[str] = field(default_factory=lambda: list(REGIONS.values()))
    #: Stop after N listings (per run, across the whole scope). None = crawl all.
    max_listings: int | None = None
    #: Cap pages per (deal, type, region) stream. None = follow to the last page.
    max_pages_per_stream: int | None = None

    def __post_init__(self) -> None:
        unknown = set(self.deals) - set(DEALS)
        if unknown:
            raise ValueError(f"unknown deals: {sorted(unknown)}")
        unknown = set(self.property_types) - set(PROPERTY_TYPES)
        if unknown:
            raise ValueError(f"unknown property types: {sorted(unknown)}")
        unknown = set(self.regions) - set(REGIONS.values())
        if unknown:
            raise ValueError(
                f"unknown regions: {sorted(unknown)}. "
                f"Only Kyrgyzstan is supported: {sorted(REGIONS.values())}"
            )
        if not self.regions:
            raise ValueError("at least one region must be enabled")


@dataclass(slots=True)
class HttpConfig:
    """HTTP behaviour: concurrency, retries, politeness."""

    workers: int = 10
    timeout: int = 30
    max_retries: int = 4
    #: Extra pause between requests inside a worker; 0 is fine at 10 workers.
    delay: float = 0.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )


@dataclass(slots=True)
class PhotoConfig:
    """Photo downloading."""

    enabled: bool = True
    #: Separate pool for image downloads (they dominate the request count).
    workers: int = 10
    #: Skip listings with more than this many photos (None = no limit).
    max_per_listing: int | None = None


@dataclass(slots=True)
class StorageConfig:
    """Where scraped data lands.

    `root` is resolved relative to the project root (the directory holding
    config.yaml), and is created on demand.
    """

    root: str = "data"
    photos_dirname: str = "photos"
    raw_dirname: str = "raw"
    state_dirname: str = "state"

    def resolve(self, project_root: Path) -> ResolvedStorage:
        root = (project_root / self.root).resolve()
        return ResolvedStorage(
            root=root,
            photos=root / self.photos_dirname,
            raw=root / self.raw_dirname,
            state=root / self.state_dirname,
        )


@dataclass(slots=True)
class ResolvedStorage:
    """Absolute paths, created on first use."""

    root: Path
    photos: Path
    raw: Path
    state: Path

    def mkdirs(self) -> None:
        for p in (self.root, self.photos, self.raw, self.state):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class HubConfig:
    """HuggingFace Hub push settings."""

    push: bool = False
    repo_id: str | None = None
    private: bool = True
    #: Token is read from the environment / `hf auth login`; never put it in YAML.
    token_env: str = "HF_TOKEN"

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env)


@dataclass(slots=True)
class DatasetConfig:
    """How the HuggingFace dataset is built."""

    output_dir: str = "hf_dataset"
    #: Photos are embedded as a HF `Image` feature and sharded, so a 46 GB image
    #: set stays loadable without a repo full of loose files.
    include_photos: bool = True
    max_shard_size: str = "500MB"
    hub: HubConfig = field(default_factory=HubConfig)


@dataclass(slots=True)
class LoggingConfig:
    """Logging and progress display."""

    level: str = "INFO"
    dirname: str = "logs"
    #: Rich progress bars. Disable for plain CI logs.
    progress: bool = True
    #: Colour the console output.
    color: bool = True


@dataclass(slots=True)
class Config:
    """Root configuration object."""

    project_root: Path = field(default_factory=Path.cwd)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    photos: PhotoConfig = field(default_factory=PhotoConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> Config:
        """Build a Config from a YAML file.

        Unknown keys raise, so a typo in the YAML fails loudly instead of being
        silently ignored.
        """
        path = Path(path).resolve()
        raw: dict[str, Any] = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        hub = HubConfig(**(raw.get("dataset", {}).pop("hub", {}) or {}))
        return cls(
            project_root=path.parent,
            scope=ScopeConfig(**(raw.get("scope") or {})),
            http=HttpConfig(**(raw.get("http") or {})),
            photos=PhotoConfig(**(raw.get("photos") or {})),
            storage=StorageConfig(**(raw.get("storage") or {})),
            dataset=DatasetConfig(hub=hub, **(raw.get("dataset") or {})),
            logging=LoggingConfig(**(raw.get("logging") or {})),
        )

    @property
    def paths(self) -> ResolvedStorage:
        """Absolute, created storage paths."""
        p = self.storage.resolve(self.project_root)
        p.mkdirs()
        return p

    @property
    def log_dir(self) -> Path:
        d = (self.project_root / self.logging.dirname).resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def dataset_dir(self) -> Path:
        return (self.project_root / self.dataset.output_dir).resolve()
