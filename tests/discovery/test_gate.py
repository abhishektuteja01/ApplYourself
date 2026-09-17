"""The shared pre-fetch title gate. One implementation for every source that
pays per row, so LinkedIn's detail backfill and the ATS boards cannot drift.
"""
from src import verticals as verticals_module
from src.discovery import gate


class MockContext:
    @property
    def verticals(self):
        return verticals_module.get_config()


class TestPassingTitles:
    def test_verdicts_align_with_the_input(self):
        items = [
            ("Widget Assembly Consultant", "example_primary"),   # include, no exclude
            ("Senior Widget Consultant", "example_primary"),     # "senior" excludes
            ("Gizmo Reconciliation Analyst", "example_primary"),
        ]
        assert gate.passing_titles(
            items, verticals_module.get_config(), "greenhouse") == [True, False, True]

    def test_strong_keep_overrides_an_exclude(self):
        items = [("Sprocket Risk Management Lead", "example_secondary")]
        assert gate.passing_titles(
            items, verticals_module.get_config(), "lever") == [True]

    def test_manual_rows_are_exempt(self):
        items = [("Senior Widget Consultant", "example_primary")]
        assert gate.passing_titles(
            items, verticals_module.get_config(), "manual") == [True]

    def test_no_items_is_no_verdicts(self):
        assert gate.passing_titles([], verticals_module.get_config(), "workday") == []

    def test_accepts_a_generator(self):
        pairs = (t for t in [("Widget Assembly Consultant", "example_primary")])
        assert gate.passing_titles(pairs, verticals_module.get_config(), "ashby") == [True]


class TestGatePassingUrls:
    def test_only_passing_rows_urls_come_back(self):
        rows = [
            {"title": "Widget Assembly Consultant", "vertical": "example_primary",
             "job_url": "https://x/1"},
            {"title": "Senior Widget Consultant", "vertical": "example_primary",
             "job_url": "https://x/2"},
        ]
        assert gate.gate_passing_urls(rows, MockContext(), "linkedin") == {"https://x/1"}

    def test_a_blank_url_is_never_returned(self):
        rows = [{"title": "Widget Assembly Consultant", "vertical": "example_primary",
                 "job_url": ""}]
        assert gate.gate_passing_urls(rows, MockContext(), "linkedin") == set()

    def test_a_url_passing_under_either_vertical_is_kept(self):
        rows = [
            {"title": "Sprocket Governance Analyst", "vertical": "example_primary",
             "job_url": "https://x/1"},
            {"title": "Sprocket Governance Analyst", "vertical": "example_secondary",
             "job_url": "https://x/1"},
        ]
        assert gate.gate_passing_urls(rows, MockContext(), "linkedin") == {"https://x/1"}
