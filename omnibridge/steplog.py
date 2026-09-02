"""
Step-by-step console logging.

The graph already prints a panel per stage, but the work inside a stage was invisible —
most of a Gemini run happens between "Discovery" and the next panel, and when it fails
there the user sees only the outcome. This narrates the steps as they happen.

Two rules it follows, for the same reason the rest of the system does:

  - Log what happened, not what is expected to happen. A step is printed when it
    completes, with its result, never as an optimistic "doing X…" that stays on screen
    if X never finished.
  - Never print a secret. Values are shown because seeing the actual router address is
    the point; anything credential-shaped is named instead.
"""

import os
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

_LEVELS = {"quiet": 0, "normal": 1, "debug": 2}


def _level() -> int:
    raw = os.environ.get("OMNIBRIDGE_LOG", "normal").strip().lower()
    return _LEVELS.get(raw, 1)


def _console():
    from rich.console import Console

    return Console(stderr=False, soft_wrap=True)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class StepLogger:
    """Prints an indented, timed trail of what a stage actually did."""

    def __init__(self, scope: str, enabled: Optional[bool] = None):
        self.scope = scope
        self.enabled = _level() >= 1 if enabled is None else enabled
        self.debug_enabled = _level() >= 2
        self._console = _console() if self.enabled else None
        self._started = time.monotonic()

    def _emit(self, marker: str, style: str, message: str) -> None:
        if not self.enabled or self._console is None:
            return
        elapsed = time.monotonic() - self._started
        self._console.print(
            f"[dim]{elapsed:6.2f}s[/dim] [{style}]{marker}[/{style}] "
            f"[dim]{self.scope}[/dim]  {message}"
        )

    def step(self, message: str) -> None:
        """A step that completed."""
        self._emit("→", "cyan", message)

    def ok(self, message: str) -> None:
        self._emit("✓", "green", message)

    def warn(self, message: str) -> None:
        self._emit("!", "yellow", message)

    def fail(self, message: str) -> None:
        self._emit("✗", "red", message)

    def detail(self, message: str) -> None:
        """Only at OMNIBRIDGE_LOG=debug. For payloads and full text."""
        if self.debug_enabled:
            self._emit(" ", "dim", f"[dim]{message}[/dim]")

    def value(self, label: str, value: Any, limit: int = 120) -> None:
        self._emit(" ", "dim", f"[dim]{label}:[/dim] {_truncate(value, limit)}")

    def bullets(self, label: str, items, limit: int = 6) -> None:
        items = list(items or [])
        if not items:
            self.value(label, "none")
            return
        self.value(label, f"{len(items)}")
        for item in items[:limit]:
            self._emit(" ", "dim", f"    [dim]·[/dim] {_truncate(item, 110)}")
        if len(items) > limit:
            self._emit(" ", "dim", f"    [dim]… {len(items) - limit} more[/dim]")

    @contextmanager
    def timed(self, message: str) -> Iterator["StepLogger"]:
        """
        Time a step and report it once it is over, with its outcome.

        Deliberately not a spinner: a line printed before the work says only that it
        was attempted, and stays on screen looking like progress when the call hangs.
        """
        start = time.monotonic()
        try:
            yield self
        except Exception as exc:
            self.fail(f"{message} — failed after {time.monotonic() - start:.2f}s: "
                      f"{_truncate(exc, 160)}")
            raise
        self.ok(f"{message} [dim]({time.monotonic() - start:.2f}s)[/dim]")
