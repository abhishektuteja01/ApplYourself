import json
import logging

from src.discovery import crawl_cursor as cc
from src.discovery.crawl_cursor import CrawlCursor, cursor_path, load_cursor, save_cursor
from src.discovery.universe import UniverseCompany


def _companies(n: int, prefix: str = "co") -> list[UniverseCompany]:
    """Workday-shaped tri-part slugs, so the `|` in them is under test."""
    return [
        UniverseCompany(name=f"Co {i}", ats="workday", slug=f"{prefix}{i}|wd1|Careers")
        for i in range(n)
    ]


_SLUG = lambda c: c.slug  # noqa: E731 — the key= workday.py actually passes


class TestRotate:
    def test_saved_slug_mid_list_starts_there_and_keeps_everything(self):
        items = list("abcde")
        cursor = CrawlCursor(ats="workday", next_slug="c")
        assert cursor.rotate(items) == ["c", "d", "e", "a", "b"]

    def test_rotation_never_drops_or_duplicates(self):
        items = list("abcdefgh")
        for slug in items:
            out = CrawlCursor(ats="workday", next_slug=slug).rotate(items)
            assert len(out) == len(items)
            assert sorted(out) == sorted(items)

    def test_missing_slug_falls_back_to_the_head(self):
        items = list("abc")
        assert CrawlCursor(ats="workday", next_slug="zz").rotate(items) == items

    def test_default_empty_slug_falls_back_to_the_head(self):
        items = list("abc")
        assert CrawlCursor(ats="workday").rotate(items) == items

    def test_empty_list_returns_empty(self):
        assert CrawlCursor(ats="workday", next_slug="a").rotate([]) == []

    def test_first_element_is_a_no_op(self):
        items = list("abc")
        assert CrawlCursor(ats="workday", next_slug="a").rotate(items) == items

    def test_last_element_puts_the_head_behind_it(self):
        items = list("abc")
        assert CrawlCursor(ats="workday", next_slug="c").rotate(items) == ["c", "a", "b"]

    def test_single_element(self):
        assert CrawlCursor(ats="workday", next_slug="a").rotate(["a"]) == ["a"]

    def test_does_not_mutate_the_input(self):
        items = list("abcd")
        CrawlCursor(ats="workday", next_slug="c").rotate(items)
        assert items == list("abcd")

    def test_key_callable_seeks_on_the_derived_value(self):
        companies = _companies(4)
        cursor = CrawlCursor(ats="workday", next_slug=companies[2].slug)
        out = cursor.rotate(companies, key=_SLUG)
        assert [c.slug for c in out] == [c.slug for c in companies[2:] + companies[:2]]

    def test_key_callable_with_an_object_not_in_the_list_falls_back(self):
        companies = _companies(3)
        cursor = CrawlCursor(ats="workday", next_slug="gone|wd1|Careers")
        assert cursor.rotate(companies, key=_SLUG) == companies

    def test_duplicate_slugs_seek_to_the_first(self):
        items = ["a", "b", "a", "c"]
        assert CrawlCursor(ats="workday", next_slug="a").rotate(items) == items


class TestAdvance:
    def test_mid_list_records_the_next_item(self):
        items = list("abcde")
        cursor = CrawlCursor(ats="workday")
        cursor.advance(items, 2)
        assert cursor.next_slug == "c"

    def test_a_completed_pass_wraps_to_the_head(self):
        items = list("abcde")
        cursor = CrawlCursor(ats="workday", next_slug="c")
        cursor.advance(items, len(items))
        assert cursor.next_slug == "a"

    def test_done_beyond_the_list_length_wraps_to_the_head(self):
        items = list("abc")
        cursor = CrawlCursor(ats="workday")
        cursor.advance(items, 99)
        assert cursor.next_slug == "a"

    def test_done_zero_records_the_head_of_the_list_given(self):
        items = list("abc")
        cursor = CrawlCursor(ats="workday", next_slug="b")
        cursor.advance(items, 0)
        assert cursor.next_slug == "a", "done=0 records the head of the list it was given"

    def test_last_item_completed_points_at_the_final_element(self):
        items = list("abcd")
        cursor = CrawlCursor(ats="workday")
        cursor.advance(items, len(items) - 1)
        assert cursor.next_slug == "d"

    def test_empty_list_neither_raises_nor_alters_the_slug(self):
        cursor = CrawlCursor(ats="workday", next_slug="keep-me")
        cursor.advance([], 0)
        cursor.advance([], 5)
        assert cursor.next_slug == "keep-me"

    def test_key_callable_records_the_derived_value(self):
        companies = _companies(4)
        cursor = CrawlCursor(ats="workday")
        cursor.advance(companies, 2, key=_SLUG)
        assert cursor.next_slug == companies[2].slug

    def test_advance_records_a_slug_rotate_can_seek_to(self):
        companies = _companies(6)
        cursor = CrawlCursor(ats="workday")
        cursor.advance(companies, 3, key=_SLUG)
        assert cursor.rotate(companies, key=_SLUG)[0].slug == companies[3].slug


class TestFairnessAcrossRuns:
    """rotate -> advance -> save -> load -> rotate makes forward progress and
    covers every tenant with a bounded visit spread."""

    def _simulate(self, n_tenants, per_run, runs, ats="workday"):
        companies = _companies(n_tenants)
        visits = {c.slug: 0 for c in companies}
        starts = []
        for _ in range(runs):
            cursor = load_cursor(ats)
            order = cursor.rotate(companies, key=_SLUG)
            starts.append(order[0].slug)
            for c in order[:per_run]:
                visits[c.slug] += 1
            cursor.advance(order, per_run, key=_SLUG)
            save_cursor(cursor)
        return visits, starts

    def test_twelve_runs_cover_every_tenant_with_a_tight_spread(self):
        visits, _ = self._simulate(n_tenants=10, per_run=4, runs=12)
        assert all(v > 0 for v in visits.values()), "every tenant must be visited"
        assert max(visits.values()) - min(visits.values()) <= 1
        assert sum(visits.values()) == 12 * 4

    def test_consecutive_runs_start_at_different_tenants(self):
        _, starts = self._simulate(n_tenants=10, per_run=4, runs=6)
        assert len(set(starts)) > 1, "the cursor must move the start point"
        assert starts[0] == _companies(10)[0].slug

    def test_per_run_batch_dividing_the_list_still_covers_everything(self):
        visits, _ = self._simulate(n_tenants=9, per_run=3, runs=6)
        assert set(visits.values()) == {2}

    def test_a_full_pass_each_run_keeps_the_start_pinned_to_the_head(self):
        """Rotation is inert when a run completes the whole list."""
        _, starts = self._simulate(n_tenants=5, per_run=5, runs=4)
        assert set(starts) == {_companies(5)[0].slug}

    def test_a_pruned_tenant_costs_one_rotation_not_correctness(self):
        companies = _companies(6)
        cursor = CrawlCursor(ats="workday")
        cursor.advance(companies, 3, key=_SLUG)
        shrunk = [c for c in companies if c.slug != companies[3].slug]
        order = cursor.rotate(shrunk, key=_SLUG)
        assert order == shrunk, "a vanished resume point falls back to the head"
        cursor.advance(order, 2, key=_SLUG)
        assert cursor.next_slug == shrunk[2].slug


class TestOffsets:
    def test_unknown_pair_defaults_to_zero(self):
        assert CrawlCursor(ats="workday").offset_for("co|wd1|C", "AI Engineer") == 0

    def test_set_offset_stores_an_int(self):
        cursor = CrawlCursor(ats="workday")
        cursor.set_offset("co|wd1|C", "AI Engineer", 50)
        assert cursor.offset_for("co|wd1|C", "AI Engineer") == 50
        assert all(isinstance(v, int) for v in cursor.offsets.values())

    def test_a_float_offset_is_coerced_to_int(self):
        cursor = CrawlCursor(ats="workday")
        cursor.set_offset("co|wd1|C", "t", 50.9)
        assert cursor.offsets[CrawlCursor._key("co|wd1|C", "t")] == 50

    def test_zero_drops_the_key(self):
        cursor = CrawlCursor(ats="workday")
        cursor.set_offset("co|wd1|C", "t", 50)
        cursor.set_offset("co|wd1|C", "t", 0)
        assert cursor.offsets == {}
        assert cursor.offset_for("co|wd1|C", "t") == 0

    def test_zero_on_an_absent_key_is_safe(self):
        cursor = CrawlCursor(ats="workday")
        cursor.set_offset("never|wd1|C", "t", 0)
        assert cursor.offsets == {}

    def test_offsets_are_per_pair(self):
        cursor = CrawlCursor(ats="workday")
        cursor.set_offset("a|wd1|C", "t1", 10)
        cursor.set_offset("a|wd1|C", "t2", 20)
        cursor.set_offset("b|wd1|C", "t1", 30)
        assert cursor.offset_for("a|wd1|C", "t1") == 10
        assert cursor.offset_for("a|wd1|C", "t2") == 20
        assert cursor.offset_for("b|wd1|C", "t1") == 30
        assert len(cursor.offsets) == 3

    def test_a_string_offset_read_back_as_int(self):
        cursor = CrawlCursor(ats="workday", offsets={CrawlCursor._key("a", "t"): "42"})
        assert cursor.offset_for("a", "t") == 42


class TestKey:
    def test_pipes_in_the_slug_do_not_collide(self):
        assert CrawlCursor._key("a|b", "c") != CrawlCursor._key("a", "b|c")

    def test_a_pipe_in_the_term_does_not_collide(self):
        cursor = CrawlCursor(ats="workday")
        cursor.set_offset("co|wd1|C", "AI|Engineer", 10)
        cursor.set_offset("co|wd1|C|AI", "Engineer", 20)
        assert cursor.offset_for("co|wd1|C", "AI|Engineer") == 10
        assert cursor.offset_for("co|wd1|C|AI", "Engineer") == 20

    def test_empty_halves_stay_distinct(self):
        assert CrawlCursor._key("", "a") != CrawlCursor._key("a", "")

    def test_a_nul_inside_a_half_is_the_one_ambiguity(self):
        """NUL is assumed absent from both halves; nothing enforces it."""
        assert CrawlCursor._key("a\x00b", "c") == CrawlCursor._key("a", "b\x00c")

    def test_whitespace_and_unicode_terms_round_trip(self):
        cursor = CrawlCursor(ats="workday")
        cursor.set_offset("co|wd1|C", "Ingénieur  IA / ML", 5)
        save_cursor(cursor)
        assert load_cursor("workday").offset_for("co|wd1|C", "Ingénieur  IA / ML") == 5


class TestCursorPath:
    def test_per_ats(self):
        assert cursor_path("workday") != cursor_path("greenhouse")

    def test_lives_under_the_patched_cursor_dir(self, tmp_path):
        assert cursor_path("workday").parent == cc.CURSOR_DIR
        assert str(tmp_path) in str(cursor_path("workday"))

    def test_two_ats_cursors_do_not_share_state(self):
        a = CrawlCursor(ats="workday", next_slug="w1")
        b = CrawlCursor(ats="greenhouse", next_slug="g1")
        save_cursor(a)
        save_cursor(b)
        assert load_cursor("workday").next_slug == "w1"
        assert load_cursor("greenhouse").next_slug == "g1"


class TestLoadCursor:
    def test_absent_file_is_a_fresh_cursor(self):
        cursor = load_cursor("workday")
        assert cursor.ats == "workday"
        assert cursor.next_slug == ""
        assert cursor.offsets == {}

    def _write(self, ats, text):
        cc.CURSOR_DIR.mkdir(parents=True, exist_ok=True)
        cursor_path(ats).write_text(text, encoding="utf-8")

    def test_corrupt_json_warns_and_resets(self, caplog):
        self._write("workday", "{not json")
        with caplog.at_level(logging.WARNING, logger=cc.log.name):
            cursor = load_cursor("workday")
        assert cursor.next_slug == ""
        assert "unreadable" in caplog.text

    def test_wrong_schema_version_resets_silently(self, caplog):
        self._write("workday", json.dumps(
            {"schema_version": 99, "next_slug": "x", "offsets": {"a\x00t": 5}}))
        with caplog.at_level(logging.WARNING, logger=cc.log.name):
            cursor = load_cursor("workday")
        assert cursor.next_slug == ""
        assert cursor.offsets == {}
        assert caplog.text == ""

    def test_missing_schema_version_resets(self):
        self._write("workday", json.dumps({"next_slug": "x"}))
        assert load_cursor("workday").next_slug == ""

    def test_a_valid_file_loads(self):
        self._write("workday", json.dumps({
            "schema_version": 1,
            "next_slug": "co3|wd1|Careers",
            "offsets": {"co1|wd1|Careers\x00AI Engineer": 100},
        }))
        cursor = load_cursor("workday")
        assert cursor.next_slug == "co3|wd1|Careers"
        assert cursor.offset_for("co1|wd1|Careers", "AI Engineer") == 100

    def test_numeric_string_offsets_are_coerced(self):
        self._write("workday", json.dumps(
            {"schema_version": 1, "offsets": {"a\x00t": "150"}}))
        assert load_cursor("workday").offset_for("a", "t") == 150

    def test_non_numeric_offsets_reset_rather_than_raise(self, caplog):
        self._write("workday", json.dumps(
            {"schema_version": 1, "next_slug": "x", "offsets": {"a\x00t": "deep"}}))
        with caplog.at_level(logging.WARNING, logger=cc.log.name):
            cursor = load_cursor("workday")
        assert cursor.next_slug == ""
        assert cursor.offsets == {}
        assert "unreadable" in caplog.text

    def test_offsets_as_a_list_resets_rather_than_raise(self):
        self._write("workday", json.dumps(
            {"schema_version": 1, "offsets": ["a", "b"]}))
        assert load_cursor("workday").offsets == {}

    def test_offsets_null_is_treated_as_empty(self):
        self._write("workday", json.dumps(
            {"schema_version": 1, "next_slug": "x", "offsets": None}))
        cursor = load_cursor("workday")
        assert cursor.next_slug == "x"
        assert cursor.offsets == {}

    def test_next_slug_null_becomes_empty_string(self):
        self._write("workday", json.dumps({"schema_version": 1, "next_slug": None}))
        assert load_cursor("workday").next_slug == ""

    def test_a_non_object_top_level_resets(self):
        for text in ("null", "5", '["a"]', '"str"'):
            self._write("workday", text)
            assert load_cursor("workday").next_slug == ""

    def test_empty_file_resets(self):
        self._write("workday", "")
        assert load_cursor("workday").next_slug == ""

    def test_an_unreadable_path_resets(self):
        """A directory where the cursor file should be: exists() is true, the
        read is an OSError."""
        cc.CURSOR_DIR.mkdir(parents=True, exist_ok=True)
        cursor_path("workday").mkdir()
        assert load_cursor("workday").next_slug == ""

    def test_nul_keys_survive_a_json_round_trip(self):
        cursor = CrawlCursor(ats="workday")
        cursor.set_offset("co|wd1|Careers", "AI Engineer", 250)
        save_cursor(cursor)
        raw = json.loads(cursor_path("workday").read_text(encoding="utf-8"))
        assert list(raw["offsets"]) == ["co|wd1|Careers\x00AI Engineer"]
        assert load_cursor("workday").offsets == cursor.offsets


class TestSaveCursor:
    def test_writes_valid_json_load_reads_back_identically(self):
        cursor = CrawlCursor(ats="workday", next_slug="co2|wd1|Careers")
        cursor.set_offset("co1|wd1|Careers", "AI Engineer", 50)
        cursor.set_offset("co2|wd1|Careers", "ML Engineer", 100)
        save_cursor(cursor)

        raw = json.loads(cursor_path("workday").read_text(encoding="utf-8"))
        assert raw["schema_version"] == 1

        back = load_cursor("workday")
        assert back.next_slug == cursor.next_slug
        assert back.offsets == cursor.offsets

    def test_creates_the_cursor_dir(self):
        assert not cc.CURSOR_DIR.exists()
        save_cursor(CrawlCursor(ats="workday"))
        assert cursor_path("workday").is_file()

    def test_rewritten_whole_not_merged(self):
        first = CrawlCursor(ats="workday")
        first.set_offset("a", "t", 10)
        save_cursor(first)
        save_cursor(CrawlCursor(ats="workday", next_slug="b"))
        back = load_cursor("workday")
        assert back.offsets == {}
        assert back.next_slug == "b"

    def test_a_failed_write_does_not_raise(self, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("read-only")

        monkeypatch.setattr(cc.Path, "write_text", boom)
        save_cursor(CrawlCursor(ats="workday", next_slug="x"))

    def test_a_failed_write_warns(self, monkeypatch, caplog):
        monkeypatch.setattr(
            cc.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
        with caplog.at_level(logging.WARNING, logger=cc.log.name):
            save_cursor(CrawlCursor(ats="workday"))
        assert "could not write crawl cursor" in caplog.text

    def test_a_file_in_place_of_the_cursor_dir_does_not_raise(self, tmp_path, monkeypatch):
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(cc, "CURSOR_DIR", blocker / "cursors")
        save_cursor(CrawlCursor(ats="workday"))
        assert load_cursor("workday").next_slug == ""


def test_the_full_loop_survives_a_corrupt_write_between_runs():
    companies = _companies(8)
    cursor = load_cursor("workday")
    order = cursor.rotate(companies, key=_SLUG)
    cursor.advance(order, 3, key=_SLUG)
    save_cursor(cursor)

    cursor_path("workday").write_text("{{{", encoding="utf-8")
    assert load_cursor("workday").rotate(companies, key=_SLUG) == companies


def test_every_entry_point_is_total_on_degenerate_input():
    cursor = CrawlCursor(ats="workday")
    cursor.rotate([])
    cursor.advance([], 0)
    cursor.offset_for("", "")
    cursor.set_offset("", "", 0)
    save_cursor(cursor)
    load_cursor("workday")
