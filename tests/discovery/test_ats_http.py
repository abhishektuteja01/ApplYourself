"""Direct coverage for fetch_json's retry/backoff path.

Every other test monkeypatches fetch_json away, so these are the only tests that
execute the retry loop. No real network call: requests.get and time.sleep are
both replaced.
"""
import pytest
import requests

from src.discovery.sources.ats import http
from src.discovery.sources.ats.http import CareersError, fetch_json


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, json_exc=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._json_exc = json_exc

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._payload


@pytest.fixture
def sleeps(monkeypatch):
    """Record every delay fetch_json sleeps for, without sleeping."""
    recorded = []
    monkeypatch.setattr(http.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def responses(monkeypatch):
    """Serve a queued list of FakeResponse/exceptions; record the call count."""
    calls = []

    def install(queue):
        def fake_get(url, timeout=None, headers=None):
            calls.append(url)
            item = queue[min(len(calls) - 1, len(queue) - 1)]
            if isinstance(item, Exception):
                raise item
            return item
        monkeypatch.setattr(http.requests, "get", fake_get)
        return calls

    return install


def test_200_returns_payload_without_sleeping(responses, sleeps):
    calls = responses([FakeResponse(200, {"jobs": [1]})])
    assert fetch_json("https://x/board") == {"jobs": [1]}
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("exc", [
    requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0),
    ValueError("no json"),
])
def test_non_json_200_raises_careers_error_without_retrying(responses, sleeps, exc):
    calls = responses([FakeResponse(200, json_exc=exc)])
    with pytest.raises(CareersError, match="invalid JSON body"):
        fetch_json("https://x/wall")
    assert len(calls) == 1
    assert sleeps == []


def test_404_raises_immediately_without_retrying(responses, sleeps):
    calls = responses([FakeResponse(404)])
    with pytest.raises(CareersError, match="board not found"):
        fetch_json("https://x/badslug")
    assert len(calls) == 1
    assert sleeps == []


def test_non_retryable_status_raises_immediately(responses, sleeps):
    calls = responses([FakeResponse(403)])
    with pytest.raises(CareersError, match="HTTP 403"):
        fetch_json("https://x/board")
    assert len(calls) == 1
    assert sleeps == []


def test_retry_after_is_capped(responses, sleeps):
    responses([
        FakeResponse(429, headers={"Retry-After": "3600"}),
        FakeResponse(200, {"ok": True}),
    ])
    assert fetch_json("https://x/board") == {"ok": True}
    assert sleeps == [http.MAX_RETRY_AFTER]


def test_retry_after_below_cap_is_honored_verbatim(responses, sleeps):
    responses([
        FakeResponse(429, headers={"Retry-After": "5"}),
        FakeResponse(200, {"ok": True}),
    ])
    assert fetch_json("https://x/board") == {"ok": True}
    assert sleeps == [5.0]


@pytest.mark.parametrize("header", ["Wed, 05 Aug 2026 07:28:00 GMT", "1.5", " 30", ""])
def test_unparseable_retry_after_falls_back_to_exponential(responses, sleeps, header):
    responses([
        FakeResponse(503, headers={"Retry-After": header}),
        FakeResponse(200, {"ok": True}),
    ])
    assert fetch_json("https://x/board") == {"ok": True}
    assert sleeps == [http.RETRY_BASE_DELAY * 2]


def test_missing_retry_after_backs_off_exponentially(responses, sleeps):
    responses([FakeResponse(500), FakeResponse(500), FakeResponse(200, {"ok": True})])
    assert fetch_json("https://x/board") == {"ok": True}
    assert sleeps == [http.RETRY_BASE_DELAY * 2, http.RETRY_BASE_DELAY * 4]


def test_retries_are_exhausted_after_max_retries(responses, sleeps):
    calls = responses([FakeResponse(500)])
    with pytest.raises(CareersError, match=f"HTTP 500 after {http.MAX_RETRIES} attempts"):
        fetch_json("https://x/board")
    assert len(calls) == http.MAX_RETRIES
    assert len(sleeps) == http.MAX_RETRIES - 1


def test_request_exception_retries_then_succeeds(responses, sleeps):
    responses([requests.ConnectionError("reset"), FakeResponse(200, {"ok": True})])
    assert fetch_json("https://x/board") == {"ok": True}
    assert sleeps == [http.RETRY_BASE_DELAY]


def test_request_exception_exhausts_and_wraps(responses, sleeps):
    calls = responses([requests.Timeout("timed out")])
    with pytest.raises(CareersError, match="fetch failed"):
        fetch_json("https://x/board")
    assert len(calls) == http.MAX_RETRIES
    assert len(sleeps) == http.MAX_RETRIES - 1


def test_deadline_crossing_retry_raises_without_sleeping(responses, sleeps, monkeypatch):
    monkeypatch.setattr(http.time, "time", lambda: 1000.0)
    responses([FakeResponse(429, headers={"Retry-After": "60"})])
    with pytest.raises(CareersError, match="deadline reached before retry"):
        fetch_json("https://x/board", deadline_ts=1030.0)
    assert sleeps == []


def test_deadline_with_room_still_sleeps(responses, sleeps, monkeypatch):
    monkeypatch.setattr(http.time, "time", lambda: 1000.0)
    responses([
        FakeResponse(429, headers={"Retry-After": "10"}),
        FakeResponse(200, {"ok": True}),
    ])
    assert fetch_json("https://x/board", deadline_ts=9999.0) == {"ok": True}
    assert sleeps == [10.0]


def test_deadline_ts_zero_means_no_deadline(responses, sleeps):
    responses([FakeResponse(429, headers={"Retry-After": "60"}), FakeResponse(200, {})])
    assert fetch_json("https://x/board", deadline_ts=0.0) == {}
    assert sleeps == [http.MAX_RETRY_AFTER]


@pytest.mark.parametrize("queue, status, permanent", [
    ([FakeResponse(404)], 404, True),
    ([FakeResponse(200, json_exc=ValueError("no json"))], 200, True),
    ([FakeResponse(403)], 403, False),
    ([FakeResponse(401)], 401, False),
    ([FakeResponse(500)], 500, False),
    ([FakeResponse(429)], 429, False),
    ([requests.Timeout("timed out")], None, False),
    ([requests.ConnectionError("reset")], None, False),
])
def test_error_carries_structured_status_and_permanence(responses, sleeps, queue, status, permanent):
    """Only a 404 or a non-JSON 200 is permanent; nothing else may prune a board."""
    responses(queue)
    with pytest.raises(CareersError) as exc_info:
        fetch_json("https://x/board")
    assert (exc_info.value.status, exc_info.value.permanent) == (status, permanent)


def test_deadline_error_is_not_permanent(responses, sleeps, monkeypatch):
    monkeypatch.setattr(http.time, "time", lambda: 1000.0)
    responses([FakeResponse(429, headers={"Retry-After": "60"})])
    with pytest.raises(CareersError) as exc_info:
        fetch_json("https://x/board", deadline_ts=1030.0)
    assert exc_info.value.permanent is False


def test_status_404_is_not_inferred_from_a_url_containing_404(responses, sleeps):
    """The old `"404" in str(e)` sniff misread any URL or slug with 404 in it."""
    responses([FakeResponse(500)])
    with pytest.raises(CareersError) as exc_info:
        fetch_json("https://x/boards/acme-404/jobs")
    assert exc_info.value.status == 500
    assert exc_info.value.permanent is False
