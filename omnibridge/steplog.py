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
import queue
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, List, Optional

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


# ---------------------------------------------------------------- the log bus
#
# The console trail is the only view of a run that takes a minute or more, and it was
# only visible to whoever started the server. The bus lets a browser subscribe to the
# same records as they happen.
#
# Records are published, not the formatted line: the terminal wants rich markup and the
# browser wants structure, and rendering once for both produces something wrong for each.

_MARKUP = re.compile(r"\[/?[a-zA-Z#][^\]]*\]")


def strip_markup(text: str) -> str:
    """Rich markup out. The browser styles from the record's fields instead."""
    return _MARKUP.sub("", str(text))


class LogBus:
    """Fan-out of step records to whoever is watching a given run."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List["queue.Queue"]] = {}
        self._lock = threading.Lock()

    def subscribe(self, run_id: str) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue(maxsize=2000)
        with self._lock:
            self._subscribers.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: "queue.Queue") -> None:
        with self._lock:
            watchers = self._subscribers.get(run_id) or []
            if q in watchers:
                watchers.remove(q)
            if not watchers:
                self._subscribers.pop(run_id, None)

    def publish(self, run_id: str, record: Dict[str, Any]) -> None:
        with self._lock:
            watchers = list(self._subscribers.get(run_id) or [])
        for q in watchers:
            try:
                q.put_nowait(record)
            except queue.Full:
                # A watcher that cannot keep up is dropped from this record, never
                # blocking the run. Logging must not be able to stall a transfer.
                pass

    def has_watchers(self, run_id: str) -> bool:
        with self._lock:
            return bool(self._subscribers.get(run_id))


LOG_BUS = LogBus()

#: The run the current thread's log records belong to, if any.
_CURRENT_RUN: ContextVar[Optional[str]] = ContextVar("omnibridge_run_id", default=None)


@contextmanager
def run_context(run_id: Optional[str]) -> Iterator[None]:
    """Attribute every step record emitted inside this block to `run_id`."""
    token = _CURRENT_RUN.set(run_id)
    try:
        yield
    finally:
        _CURRENT_RUN.reset(token)


def current_run() -> Optional[str]:
    return _CURRENT_RUN.get()


class StepLogger:
    """Prints an indented, timed trail of what a stage actually did."""

    def __init__(self, scope: str, enabled: Optional[bool] = None):
        self.scope = scope
        self.enabled = _level() >= 1 if enabled is None else enabled
        self.debug_enabled = _level() >= 2
        self._console = _console() if self.enabled else None
        self._started = time.monotonic()

    def _emit(self, marker: str, style: str, message: str) -> None:
        elapsed = time.monotonic() - self._started

        # Published first, and regardless of the console level. A browser watching a
        # run should not go blank because the server was started with OMNIBRIDGE_LOG=quiet.
        run_id = current_run()
        if run_id:
            LOG_BUS.publish(run_id, {
                "t": round(elapsed, 2),
                "marker": marker.strip() or "·",
                "level": style,
                "scope": self.scope,
                "message": strip_markup(message),
            })

        if not self.enabled or self._console is None:
            return
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
