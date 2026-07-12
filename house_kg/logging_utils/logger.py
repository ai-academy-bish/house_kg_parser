"""Logging setup: rich colour console + rotating file, no `print` anywhere.

Every module gets its logger via `get_logger(__name__)`. `setup_logging()` is
called once, from the entry point, and wires both sinks:

* console — colourised, human-scannable, plays nicely with the progress bars;
* file    — full detail (timestamps, module, level), one file per run, so a
            crashed 3-hour crawl can still be post-mortemed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

#: Shared console — the progress bars render through this same object, which is
#: what keeps log lines from tearing through a live bar.
THEME = Theme(
    {
        "logging.level.debug": "dim cyan",
        "logging.level.info": "bold cyan",
        "logging.level.warning": "bold yellow",
        "logging.level.error": "bold red",
        "logging.level.critical": "bold white on red",
    }
)

CONSOLE = Console(theme=THEME, stderr=False)

_CONFIGURED = False


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    color: bool = True,
    run_name: str = "run",
) -> Path:
    """Configure root logging. Returns the path of the log file.

    Idempotent: calling it twice will not double every log line.
    """
    global _CONFIGURED

    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"{run_name}_{stamp}.log"

    root = logging.getLogger()
    if _CONFIGURED:
        return log_file
    root.setLevel(logging.DEBUG)

    console_handler = RichHandler(
        console=CONSOLE,
        rich_tracebacks=True,
        markup=color,
        show_path=False,
        omit_repeated_times=False,
        log_time_format="[%H:%M:%S]",
    )
    console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    console_handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # the file always keeps everything
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    # third-party noise stays in the file, off the console
    for noisy in ("urllib3", "requests", "filelock", "huggingface_hub", "datasets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger(__name__).debug("logging initialised -> %s", log_file)
    return log_file


def get_logger(name: str) -> logging.Logger:
    """Module-level logger."""
    return logging.getLogger(name)
