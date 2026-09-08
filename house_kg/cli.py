"""Command-line entry points (used by the Makefile targets)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config
from .crawler import Pipeline
from .dataset import Bootstrapper, HFDatasetBuilder
from .logging_utils import ProgressTracker, get_logger, setup_logging
from .storage import Storage
from .validate import Validator

logger = get_logger(__name__)


def _load(args: argparse.Namespace) -> Config:
    config = Config.load(args.config)
    if getattr(args, "limit", None):
        config.scope.max_listings = args.limit
    if getattr(args, "workers", None):
        config.http.workers = args.workers
    if getattr(args, "no_photos", False):
        config.photos.enabled = False
    if getattr(args, "no_progress", False):
        config.logging.progress = False
    if getattr(args, "full", False):
        config.refresh.detail_refresh = "all"
    if getattr(args, "first_run", False):
        config.dataset.is_first_run = True
    if getattr(args, "no_push", False):
        config.dataset.hub.push = False
    return config


def cmd_crawl(args: argparse.Namespace) -> int:
    config = _load(args)
    log_file = setup_logging(
        config.log_dir, config.logging.level, config.logging.color, run_name="crawl"
    )
    logger.info("logging to %s", log_file)
    report = Pipeline(config, snapshot_id=args.snapshot_id).run()
    # a scheduled run is judged by whether the sweep was complete, not by whether
    # the process exited — an incomplete snapshot must not pass silently in CI
    return 0 if report.complete else 2


def cmd_pull(args: argparse.Namespace) -> int:
    """Restore local crawl state from the published dataset."""
    config = _load(args)
    setup_logging(config.log_dir, config.logging.level, config.logging.color, run_name="pull")
    Bootstrapper(config).run(force=args.force)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    config = _load(args)
    log_file = setup_logging(
        config.log_dir, config.logging.level, config.logging.color, run_name="dataset"
    )
    logger.info("logging to %s", log_file)

    paths = config.paths
    storage = Storage(raw_dir=paths.raw, photos_dir=paths.photos)
    if not len(storage.listings):
        logger.error("no listings found in %s — run the crawler first", paths.raw)
        return 1

    with ProgressTracker(enabled=config.logging.progress) as progress:
        HFDatasetBuilder(config, storage, progress).build()
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    config = _load(args)
    setup_logging(config.log_dir, config.logging.level, config.logging.color, run_name="validate")
    paths = config.paths
    storage = Storage(raw_dir=paths.raw, photos_dir=paths.photos)
    return 0 if Validator(storage).run() else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="house-kg", description="house.kg scraper and dataset builder"
    )
    parser.add_argument(
        "-c", "--config", default=str(Path("config.yaml")), help="path to config.yaml"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="take a snapshot of the board")
    crawl.add_argument("--limit", type=int, help="stop after N listings")
    crawl.add_argument("--workers", type=int, help="override HTTP workers")
    crawl.add_argument("--no-photos", action="store_true", help="skip photo downloads")
    crawl.add_argument("--no-progress", action="store_true", help="plain logs, no bars")
    crawl.add_argument(
        "--snapshot-id", help="snapshot label (default: today's date, e.g. 2026-09-08)"
    )
    crawl.add_argument(
        "--full",
        action="store_true",
        help="re-read every detail page, not just new listings (~10x the requests)",
    )
    crawl.add_argument(
        "--first-run",
        action="store_true",
        help="force baseline mode: do not diff against earlier snapshots",
    )
    crawl.set_defaults(func=cmd_crawl)

    pull = sub.add_parser(
        "pull", help="restore local state from the published dataset (no photos)"
    )
    pull.add_argument(
        "--force", action="store_true", help="overwrite local tables if present"
    )
    pull.set_defaults(func=cmd_pull)

    build = sub.add_parser("build", help="package the HuggingFace dataset")
    build.add_argument("--no-progress", action="store_true")
    build.add_argument("--no-push", action="store_true", help="build locally, do not upload")
    build.set_defaults(func=cmd_build)

    validate = sub.add_parser("validate", help="check keys, foreign keys and photos")
    validate.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
