"""Multi-track progress display.

The crawl runs several stages, each with its own rhythm — URL discovery, listing
pages, photo downloads, entity profiles, dataset shards. A single bar would hide
all of that, so `ProgressTracker` gives every stage its own colour-coded bar and
keeps them on screen together.

The tracker shares one `Console` with the logger, so a log line printed mid-crawl
does not tear through a live bar.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from .logger import CONSOLE


@dataclass(frozen=True, slots=True)
class TrackStyle:
    """Colour scheme for one progress track."""

    colour: str
    icon: str


#: One visual identity per stage, so a glance at the screen tells you which part
#: of a multi-hour crawl is currently moving.
STYLES: dict[str, TrackStyle] = {
    "urls": TrackStyle("cyan", "🔎"),
    "listings": TrackStyle("green", "🏠"),
    "photos": TrackStyle("magenta", "📷"),
    "companies": TrackStyle("yellow", "🏢"),
    "complexes": TrackStyle("blue", "🏗"),
    "users": TrackStyle("bright_magenta", "👤"),
    "dataset": TrackStyle("bright_green", "📦"),
    "default": TrackStyle("white", "•"),
}


class ColouredBarColumn(BarColumn):
    """A bar that takes its colour from the task itself.

    `BarColumn(complete_style=...)` wants a real style, not a format string, so
    per-track colours have to be applied at render time rather than declared once.
    """

    def render(self, task) -> object:  # type: ignore[override]
        self.complete_style = task.fields.get("colour", "white")
        return super().render(task)


class ProgressTracker:
    """Owns a `rich.Progress` with one task per stage.

    Use as a context manager; call `track()` to register a stage and `advance()`
    as work completes. When `enabled` is False every method is a no-op, which
    keeps CI logs clean without the callers needing an `if`.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._tasks: dict[str, TaskID] = {}
        self._progress = Progress(
            SpinnerColumn(style="bright_black"),
            TextColumn("[bold]{task.fields[icon]}[/] {task.description:<22}"),
            ColouredBarColumn(bar_width=34, finished_style="bright_green"),
            MofNCompleteColumn(),
            TextColumn("[bright_black]{task.percentage:>5.1f}%[/]"),
            TimeElapsedColumn(),
            TextColumn("[bright_black]eta[/]"),
            TimeRemainingColumn(),
            console=CONSOLE,
            transient=False,
            refresh_per_second=8,
        )

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> ProgressTracker:
        if self.enabled:
            self._progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.enabled:
            self._progress.stop()

    # -- tracks ------------------------------------------------------------

    def track(self, name: str, total: int | None, description: str | None = None) -> None:
        """Register a stage, or re-purpose an existing one (new total + label).

        A track is reused across phases (URL discovery sizes the streams, then
        walks their pages), so the description must move with it — otherwise the
        bar keeps advertising the phase that already finished.
        """
        if not self.enabled:
            return
        style = STYLES.get(name, STYLES["default"])
        if name in self._tasks:
            self._progress.reset(
                self._tasks[name],
                total=total,
                description=description or name,
            )
            return
        self._tasks[name] = self._progress.add_task(
            description or name,
            total=total,
            icon=style.icon,
            colour=style.colour,
        )

    def advance(self, name: str, step: int = 1) -> None:
        if not self.enabled or name not in self._tasks:
            return
        self._progress.advance(self._tasks[name], step)

    def update_total(self, name: str, total: int) -> None:
        if not self.enabled or name not in self._tasks:
            return
        self._progress.update(self._tasks[name], total=total)

    def complete(self, name: str) -> None:
        """Mark a track finished even if its total was only an estimate."""
        if not self.enabled or name not in self._tasks:
            return
        task = self._progress.tasks[self._progress.task_ids.index(self._tasks[name])]
        self._progress.update(self._tasks[name], completed=task.total or task.completed)

    @contextmanager
    def stage(self, name: str, total: int | None, description: str | None = None) -> Iterator[None]:
        """Convenience: register a track and mark it complete on exit."""
        self.track(name, total, description)
        try:
            yield
        finally:
            self.complete(name)
