"""TEMPORARY — delete alongside src/discovery/trace.py.

Two guarantees only: the flag really silences it, and a loop shorter than the
tick interval stays quiet. Everything else is a log line with no control flow
behind it and is not worth pinning.
"""
import logging

from src.discovery import trace


def test_trace_is_silent_when_the_flag_is_off(monkeypatch, caplog):
    monkeypatch.setattr(trace, "TRACE", False)
    ticker = trace.Ticker("greenhouse", total=1000, every=1)
    with caplog.at_level(logging.INFO):
        trace.trace("hello")
        ticker.tick(1)
        ticker.finish(1)
    assert caplog.text == ""


def test_a_loop_shorter_than_the_interval_never_ticks(monkeypatch, caplog):
    # Explicit, not inherited from the module default: flipping TRACE off must
    # stay a safe one-line edit, so no test may depend on it being on.
    monkeypatch.setattr(trace, "TRACE", True)
    ticker = trace.Ticker("ashby", total=10, every=250)
    with caplog.at_level(logging.INFO):
        for done in range(1, 11):
            ticker.tick(done)
    assert "TRACE" not in caplog.text

    # finish() always reports, so a short loop is still accounted for.
    with caplog.at_level(logging.INFO):
        ticker.finish(10, ok=10)
    assert "ashby 10/10 ok=10" in caplog.text
