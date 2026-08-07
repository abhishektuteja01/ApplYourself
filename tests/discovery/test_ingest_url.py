"""Tests for src/discovery/ingest_url.py — URL parsing, per-ATS fetch
dispatch, the generic-HTML fallback, and the ingest-to-clean.parquet flow.
No test performs a real network call: careers.fetch_json / requests.get /
fetch_row are always monkeypatched."""
from __future__ import annotations

import pandas as pd
import pytest

from src.discovery import cleaning, ingest_url
from src.discovery.sources.ats import http
from src.discovery.schema import make_row
from src.discovery.ingest_url import (
    IngestError,
    fetch_row,
    ingest,
    parse_ats_url,
)

_JD = ("Responsibilities include widget assembly contract settlement support and "
       "position reporting. " * 5)  # comfortably over the 200-char floor


def _row(company="Acme Corp", title="Widget Assembly Analyst", jd=_JD,
         url="https://boards.greenhouse.io/acme/jobs/123", location=""):
    return make_row(site="greenhouse", company=company, title=title, job_url=url,
                    description=jd, location=location)


def _dirs(tmp_path):
    return dict(raw_dir=tmp_path / "jobs" / "raw", clean_dir=tmp_path / "jobs",
                runs_dir=tmp_path / "jobs" / "runs",
                pipeline_dir=tmp_path / "pipeline")


# ---------- parse_ats_url ----------

class TestParseAtsUrl:
    def test_greenhouse_hosts(self):
        for host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
            assert parse_ats_url(f"https://{host}/acme/jobs/4567?t=x") == \
                ("greenhouse", "acme", "4567")

    def test_greenhouse_embed(self):
        url = "https://boards.greenhouse.io/embed/job_app?for=acme&token=99"
        assert parse_ats_url(url) == ("greenhouse", "acme", "99")

    def test_lever(self):
        uuid = "12345678-abcd-4bcd-8bcd-1234567890ab"
        assert parse_ats_url(f"https://jobs.lever.co/acme/{uuid}/apply") == \
            ("lever", "acme", uuid)
        assert parse_ats_url(f"https://jobs.eu.lever.co/acme/{uuid}") == \
            ("lever:eu", "acme", uuid)

    def test_ashby(self):
        uuid = "12345678-abcd-4bcd-8bcd-1234567890ab"
        assert parse_ats_url(f"https://jobs.ashbyhq.com/acme/{uuid}") == \
            ("ashby", "acme", uuid)

    def test_unrecognized(self):
        assert parse_ats_url("https://careers.example.com/job/42") is None
        assert parse_ats_url("https://jobs.lever.co/acme") is None
        assert parse_ats_url("https://boards.greenhouse.io/acme") is None


# ---------- fetch_row ----------

class TestFetchRow:
    def test_generic_requires_company_and_title(self):
        with pytest.raises(IngestError, match="--company"):
            fetch_row("https://careers.example.com/job/42")

    def test_generic_happy(self, monkeypatch):
        class Resp:
            status_code = 200
            text = f"<html><body><h1>Role</h1><p>{_JD}</p></body></html>"
        monkeypatch.setattr(ingest_url.requests, "get",
                            lambda *a, **k: Resp())
        row = fetch_row("https://careers.example.com/job/42",
                        company="Acme", title="Widget Assembly Analyst")
        assert row["site"] == "manual"
        assert row["company"] == "Acme"
        assert "contract settlement" in row["description"]

    def test_generic_http_error(self, monkeypatch):
        class Resp:
            status_code = 404
            text = ""
        monkeypatch.setattr(ingest_url.requests, "get",
                            lambda *a, **k: Resp())
        with pytest.raises(IngestError, match="HTTP 404"):
            fetch_row("https://careers.example.com/gone",
                      company="Acme", title="X")

    def test_greenhouse_fetch_and_overrides(self, monkeypatch):
        def fake_fetch_json(url, timeout=30):
            if "/jobs/123" in url:
                return {"title": "Widget Assembly Analyst", "content": f"<p>{_JD}</p>",
                        "location": {"name": "Remote"}}
            return {"name": "Acme Corp"}  # board meta
        monkeypatch.setattr(http, "fetch_json", fake_fetch_json)
        row = fetch_row("https://boards.greenhouse.io/acme/jobs/123")
        assert row["company"] == "Acme Corp"
        assert row["title"] == "Widget Assembly Analyst"
        row2 = fetch_row("https://boards.greenhouse.io/acme/jobs/123",
                         company="Acme Inc", title="Custom Title")
        assert (row2["company"], row2["title"]) == ("Acme Inc", "Custom Title")

    def test_greenhouse_board_meta_failure_falls_back_to_slug(self, monkeypatch):
        def fake_fetch_json(url, timeout=30):
            if "/jobs/123" in url:
                return {"title": "Widget Assembly Analyst", "content": f"<p>{_JD}</p>"}
            raise http.CareersError("board meta down")
        monkeypatch.setattr(http, "fetch_json", fake_fetch_json)
        row = fetch_row("https://boards.greenhouse.io/acme-corp/jobs/123")
        assert row["company"] == "Acme Corp"

    def test_ashby_matches_posting_on_board(self, monkeypatch):
        uuid = "12345678-abcd-4bcd-8bcd-1234567890ab"
        board = {"jobs": [
            {"id": "other", "title": "Nope",
             "jobUrl": "https://jobs.ashbyhq.com/acme/other"},
            {"id": uuid, "title": "Widget Assembly Analyst",
             "jobUrl": f"https://jobs.ashbyhq.com/acme/{uuid}",
             "descriptionHtml": f"<p>{_JD}</p>"},
        ]}
        monkeypatch.setattr(http, "fetch_json", lambda *a, **k: board)
        row = fetch_row(f"https://jobs.ashbyhq.com/acme/{uuid}")
        assert row["title"] == "Widget Assembly Analyst"

    def test_ashby_posting_gone(self, monkeypatch):
        uuid = "12345678-abcd-4bcd-8bcd-1234567890ab"
        monkeypatch.setattr(http, "fetch_json", lambda *a, **k: {"jobs": []})
        with pytest.raises(IngestError, match="not found"):
            fetch_row(f"https://jobs.ashbyhq.com/acme/{uuid}")

    def test_lever_fetch(self, monkeypatch):
        uuid = "12345678-abcd-4bcd-8bcd-1234567890ab"
        posting = {"text": "Widget Assembly Analyst",
                   "hostedUrl": f"https://jobs.lever.co/acme/{uuid}",
                   "descriptionPlain": _JD}
        seen = {}
        def fake_fetch_json(url, timeout=30):
            seen["url"] = url
            return posting
        monkeypatch.setattr(http, "fetch_json", fake_fetch_json)
        row = fetch_row(f"https://jobs.lever.co/acme/{uuid}")
        assert row["title"] == "Widget Assembly Analyst"
        assert seen["url"] == f"https://api.lever.co/v0/postings/acme/{uuid}"


# ---------- ingest ----------

class TestIngest:
    def test_happy_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest_url, "fetch_row", lambda *a, **k: _row())
        info = ingest("https://boards.greenhouse.io/acme/jobs/123",
                      vertical="example_primary", **_dirs(tmp_path))
        expected = cleaning.compute_job_id(
            cleaning.normalize_company("Acme Corp"),
            cleaning.normalize_title("Widget Assembly Analyst"))
        assert info["job_id"] == expected
        assert info["vertical"] == "example_primary"
        clean = pd.read_parquet(tmp_path / "jobs" / "clean.parquet")
        assert expected in set(clean["job_id"])
        assert list((tmp_path / "jobs" / "raw").glob("*.parquet"))

    def test_vertical_fallback_classifier(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest_url, "fetch_row",
                            lambda *a, **k: _row(title="Widget Assembly Consultant"))
        info = ingest("https://x", **_dirs(tmp_path))
        assert info["vertical"] == "example_primary"

    def test_unknown_vertical_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest_url, "fetch_row", lambda *a, **k: _row())
        with pytest.raises(IngestError, match="not configured"):
            ingest("https://x", vertical="nope", **_dirs(tmp_path))

    def test_unclassifiable_title_needs_vertical(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest_url, "fetch_row",
                            lambda *a, **k: _row(title="Barista"))
        with pytest.raises(IngestError, match="--vertical"):
            ingest("https://x", **_dirs(tmp_path))

    def test_short_jd_fails_before_write(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest_url, "fetch_row",
                            lambda *a, **k: _row(jd="too short"))
        with pytest.raises(IngestError, match="200-char"):
            ingest("https://x", vertical="example_primary", **_dirs(tmp_path))
        assert not (tmp_path / "jobs" / "raw").exists()

    def test_same_run_id_collision_merges(self, tmp_path, monkeypatch):
        dirs = _dirs(tmp_path)
        monkeypatch.setattr(ingest_url, "fetch_row", lambda *a, **k: _row())
        ingest("https://x", vertical="example_primary", **dirs)
        monkeypatch.setattr(ingest_url, "fetch_row",
                            lambda *a, **k: _row(company="Other Co",
                                                 title="Widget Assembly Lead"))
        ingest("https://y", vertical="example_primary", **dirs)
        clean = pd.read_parquet(tmp_path / "jobs" / "clean.parquet")
        assert len(clean) == 2  # both survive the same-minute raw file

    def test_near_duplicate_reports_winner(self, tmp_path, monkeypatch):
        dirs = _dirs(tmp_path)
        monkeypatch.setattr(ingest_url, "fetch_row",
                            lambda *a, **k: _row(jd=_JD * 3))
        ingest("https://x", vertical="example_primary", **dirs)
        winner = cleaning.compute_job_id(
            cleaning.normalize_company("Acme Corp"),
            cleaning.normalize_title("Widget Assembly Analyst"))
        # Same company, near-identical title, shorter JD -> loses dedupe. The
        # variant must not differ by a level token ("II", "Senior", "Intern"),
        # which near_dedupe now treats as a distinct role.
        monkeypatch.setattr(ingest_url, "fetch_row",
                            lambda *a, **k: _row(title="Widget Assembly Analyst Remote"))
        with pytest.raises(IngestError, match=winner):
            ingest("https://y", vertical="example_primary", **dirs)

    def test_generic_error_points_at_dry_run(self):
        with pytest.raises(IngestError, match="--dry-run"):
            fetch_row("https://careers.example.com/job/42")

    def test_generic_strips_script_and_style_bodies(self, monkeypatch):
        # SPA shells (e.g. Oracle HCM) are mostly <script>/<style>; their
        # contents must not count as JD text or they defeat the 200-char floor
        class Resp:
            status_code = 200
            text = ("<html><head><style>body{margin:0}" + "x" * 500 +
                    "</style><script>var a=1;" + "y" * 500 +
                    "</script></head><body><p>Short shell.</p></body></html>")
        monkeypatch.setattr(ingest_url.requests, "get",
                            lambda *a, **k: Resp())
        row = fetch_row("https://careers.example.com/spa",
                        company="Acme", title="X")
        assert row["description"] == "Short shell."


# ---------- fetch_text_only / --dry-run ----------

class TestDryRun:
    def test_generic_returns_empty_company_and_title(self, monkeypatch):
        class Resp:
            status_code = 200
            text = f"<html><body><p>{_JD}</p></body></html>"
        monkeypatch.setattr(ingest_url.requests, "get", lambda *a, **k: Resp())
        info = ingest_url.fetch_text_only("https://careers.example.com/job/42")
        # "" not a guess: company/title define the job_id hash.
        assert info["company"] == ""
        assert info["title"] == ""
        assert "contract settlement" in info["text"]

    def test_ats_url_returns_structured_company_and_title(self, monkeypatch):
        monkeypatch.setattr(ingest_url, "fetch_row", lambda *a, **k: _row())
        info = ingest_url.fetch_text_only(
            "https://boards.greenhouse.io/acme/jobs/123")
        assert info["company"] == "Acme Corp"
        assert info["title"] == "Widget Assembly Analyst"
        assert info["text"] == _JD

    def test_cli_dry_run_writes_nothing(self, tmp_path, monkeypatch, capsys):
        class Resp:
            status_code = 200
            text = f"<html><body><p>{_JD}</p></body></html>"
        monkeypatch.setattr(ingest_url.requests, "get", lambda *a, **k: Resp())
        monkeypatch.setattr(ingest_url, "ingest",
                            lambda *a, **k: pytest.fail("dry-run must not ingest"))
        monkeypatch.chdir(tmp_path)

        rc = ingest_url.main(["https://careers.example.com/job/42", "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "company: \n" in out and "title: \n" in out
        assert "contract settlement" in out
        assert not (tmp_path / "jobs").exists()


# ---------- location-allowlist drop hint ----------

class TestLocationDropHint:
    def test_out_of_allowlist_location_names_the_filter(self, tmp_path, monkeypatch):
        from src.discovery.config import DiscoveryConfig
        # Pin the allowlist so a local profile/discovery.yaml cannot change it.
        monkeypatch.setattr(ingest_url, "load_config", lambda *a, **k: DiscoveryConfig())
        monkeypatch.setattr(ingest_url, "fetch_row",
                            lambda *a, **k: _row(location="Berlin, Germany"))
        with pytest.raises(IngestError, match="location_allowlist"):
            ingest("https://x", vertical="example_primary", **_dirs(tmp_path))

    def test_in_allowlist_location_gets_no_location_hint(self, tmp_path, monkeypatch):
        from src.discovery.config import DiscoveryConfig
        monkeypatch.setattr(ingest_url, "load_config", lambda *a, **k: DiscoveryConfig())
        monkeypatch.setattr(ingest_url, "fetch_row",
                            lambda *a, **k: _row(location="Austin, TX"))
        info = ingest("https://x", vertical="example_primary", **_dirs(tmp_path))
        assert info["job_id"]
