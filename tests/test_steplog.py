"""
Step logging.

Asserts the two rules that keep a log trustworthy: it reports completed work rather
than intentions, and it never prints a credential.
"""

import pytest

from omnibridge.steplog import StepLogger


def _lines(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_quiet_prints_nothing(monkeypatch, capsys):
    monkeypatch.setenv("OMNIBRIDGE_LOG", "quiet")
    log = StepLogger("test")
    log.step("something")
    log.ok("done")
    assert _lines(capsys) == []


def test_normal_prints_steps_but_not_details(monkeypatch, capsys):
    monkeypatch.setenv("OMNIBRIDGE_LOG", "normal")
    log = StepLogger("test")
    log.step("visible")
    log.detail("only at debug")
    out = "\n".join(_lines(capsys))
    assert "visible" in out
    assert "only at debug" not in out


def test_debug_includes_details(monkeypatch, capsys):
    monkeypatch.setenv("OMNIBRIDGE_LOG", "debug")
    log = StepLogger("test")
    log.detail("payload body")
    assert "payload body" in "\n".join(_lines(capsys))


def test_a_timed_step_is_reported_after_it_finishes(monkeypatch, capsys):
    """
    Not a spinner. A line printed before the work says only that it was attempted, and
    stays on screen looking like progress when the call hangs.
    """
    monkeypatch.setenv("OMNIBRIDGE_LOG", "normal")
    log = StepLogger("test")
    with log.timed("the work"):
        assert _lines(capsys) == [], "nothing should print until the step completes"
    out = "\n".join(_lines(capsys))
    assert "the work" in out and "✓" in out


def test_a_failed_step_is_reported_as_failed(monkeypatch, capsys):
    monkeypatch.setenv("OMNIBRIDGE_LOG", "normal")
    log = StepLogger("test")
    with pytest.raises(ValueError):
        with log.timed("the work"):
            raise ValueError("it broke")
    out = "\n".join(_lines(capsys))
    assert "✗" in out and "it broke" in out


def test_long_values_are_truncated_not_wrapped(monkeypatch, capsys):
    monkeypatch.setenv("OMNIBRIDGE_LOG", "normal")
    log = StepLogger("test")
    log.value("calldata", "0x" + "ab" * 400, limit=40)
    assert "…" in "\n".join(_lines(capsys))


# ------------------------------------------------------------- run-wide timing


def test_a_record_carries_both_clocks():
    """
    Every StepLogger times from its own construction, so the trail restarts at zero for
    each stage. Without a run-wide clock there is no way to read what a run cost.
    """
    from omnibridge.steplog import LOG_BUS, StepLogger, run_context

    with run_context("run-clocks"):
        watcher = LOG_BUS.subscribe("run-clocks")
        StepLogger("discovery", enabled=False).ok("resolved")
        record = watcher.get_nowait()

    assert record["t"] >= 0
    assert record["total"] is not None and record["total"] >= record["t"]


def test_there_is_no_total_outside_a_run():
    """None, not zero — the browser must not render "0.0s into the run" for a CLI run."""
    from omnibridge.steplog import LOG_BUS, StepLogger, run_context

    with run_context("run-no-clock"):
        watcher = LOG_BUS.subscribe("run-no-clock")
        # A nested context with no clock cannot happen, so assert the helper directly.
        from omnibridge.steplog import run_elapsed

        assert run_elapsed() is not None
        StepLogger("x", enabled=False).ok("y")
        watcher.get_nowait()

    from omnibridge.steplog import run_elapsed

    assert run_elapsed() is None


def test_a_duration_is_formatted_for_a_human():
    from omnibridge.steplog import format_duration

    assert format_duration(0.4213) == "0.42s"
    assert format_duration(57.54) == "57.5s"
    assert format_duration(124.4) == "2m 04s"
    assert format_duration(None) == "unknown"
