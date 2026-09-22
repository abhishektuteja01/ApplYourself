"""CrawlTrace: counts every request attempt and names the first pushback."""
import json

from src.discovery.crawl_trace import CrawlTrace


def _ev(status, attempt=1, secs=0.5):
    return {"status": status, "attempt": attempt, "secs": secs,
            "method": "POST", "url": "https://acme.wd1.myworkdayjobs.com/x"}


def test_counts_statuses_retries_and_the_first_pushback(tmp_path):
    trace = CrawlTrace(tmp_path / "t.jsonl")
    for ev in [_ev(200), _ev(200), _ev(429), _ev(200, attempt=2), _ev(403)]:
        trace.on_http(ev)
    trace.add_sleep(4.0)
    trace.close()

    assert trace.requests == 5
    assert trace.by_status == {"200": 3, "429": 1, "403": 1}
    assert trace.retries == 1
    assert trace.first_block["status"] == 429
    assert trace.first_block["request"] == 3
    assert trace.first_block["per_min"] == 3

    lines = trace.summary_lines()
    assert lines[0].startswith("HTTP: 5 requests")
    assert "retries: 1" in lines[0]
    assert "4s pacing sleep" in lines[1]
    assert lines[2].startswith("First pushback: HTTP 429 at request #3")

    records = [json.loads(line) for line in (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["status"] for r in records] == [200, 200, 429, 200, 403]
    assert all(r["kind"] == "http" for r in records)


def test_a_404_is_not_pushback(tmp_path):
    trace = CrawlTrace(None)
    trace.on_http(_ev(404))
    assert trace.first_block is None
    assert "First pushback: none" in trace.summary_lines()


def test_no_path_writes_nothing_but_still_counts(tmp_path):
    trace = CrawlTrace(None)
    trace.on_http(_ev(200))
    trace.record("term", company="x")
    assert trace.requests == 1
    assert not any(line.startswith("Trace:") for line in trace.summary_lines())
