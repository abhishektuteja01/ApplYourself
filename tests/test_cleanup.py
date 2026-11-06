"""`track cleanup`: done roles lose their folders; applied ones keep a resume."""

from __future__ import annotations

from datetime import datetime

from src import app_cli, state_io, track_cli
from src.apply import sheet
from src.shared import paths

OLD = datetime(2026, 1, 1, 9, 0, 0)


def _role(job_id: str, moves: list[str], *, files=("resume.md", "jd_snapshot.md"), now=None) -> str:
    p = state_io.state_path_for(paths.PIPELINE, job_id)
    first, *rest = moves
    init = {"job_id": job_id, "company": "Acme", "title": "Engineer"}
    state_io.transition(p, first, initial_fields=init, now=now)
    for move in rest:
        state_io.transition(p, move, now=now)
    rel = f"example_primary/2026-01-01_acme_engineer_{job_id}"
    folder = app_cli.APPLICATIONS / rel
    folder.mkdir(parents=True)
    for name in files:
        (folder / name).write_text(f"{job_id} {name}\n", encoding="utf-8")
    state_io.append_tailored_dir(p, rel)
    return rel


def test_cleanup_deletes_done_folders_archives_applied_ones_and_never_moves_state(capsys):
    skip = _role("aaaa0001", ["saved", "skip"])
    never = _role("aaaa0002", ["saved", "withdrawn"])
    rejected = _role("aaaa0003", ["saved", "applied", "rejected"])
    ghosted = _role("aaaa0004", ["saved", "applied"], now=OLD)
    live = _role("aaaa0005", ["saved"])
    other = app_cli.APPLICATIONS / "example_primary" / "2026-01-01_job-fair"
    other.mkdir()

    assert track_cli.main(["cleanup"]) == 0  # read-only by default
    assert "would delete 4 folders" in capsys.readouterr().out
    assert (app_cli.APPLICATIONS / skip).is_dir()

    assert track_cli.main(["cleanup", "--apply"]) == 0
    apps = app_cli.APPLICATIONS
    for rel in (skip, never, rejected, ghosted):
        assert not (apps / rel).exists()
    assert (apps / live).is_dir() and other.is_dir()
    archive = apps / "_archive" / "example_primary"
    assert sorted(p.stem for p in archive.iterdir()) == sorted(
        rel.split("/")[1] for rel in (rejected, ghosted)
    )
    states = state_io.load_state_index(paths.PIPELINE)
    assert [states[j]["state"] for j in sorted(states)] == [
        "skip",
        "withdrawn",
        "rejected",
        "applied",
        "saved",
    ]
    assert states["aaaa0001"]["cleaned"]["archived"] is None
    assert states["aaaa0003"]["cleaned"]["archived"].endswith(rejected.split("/")[1] + ".md")
    assert "cleaned" not in states["aaaa0005"]
    text = (archive / f"{rejected.split('/')[1]}.md").read_text(encoding="utf-8")
    assert text == "aaaa0003 resume.md\n\n## Job description\n\naaaa0003 jd_snapshot.md\n"


def test_a_cleaned_role_brought_back_is_not_rebuilt_on_its_own():
    _role("aaaa0001", ["saved", "skip"])
    track_cli.main(["cleanup", "--apply"])
    track_cli.main(["aaaa0001", "saved"])
    state = state_io.load_state(state_io.state_path_for(paths.PIPELINE, "aaaa0001"))
    assert app_cli.built_folder(state) is None
    # Not New (the night's tailor) nor Resume ready (the night's /apply).
    assert sheet.status_for("aaaa0001", True, state, None, None)[0] == "Cleaned"
