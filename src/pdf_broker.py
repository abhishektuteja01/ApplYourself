"""Word conversions for the night run, done by a helper the stage's bash starts.

macOS ties Word automation to the top process of the chain. Inside a headless
`claude` session that is the versioned claude binary, a new app on every update;
the helper runs under the stage's bash, so one grant holds. `export_pdf` hands
it each conversion when PDF_BROKER_DIR is set.

  uv run python -m src.pdf_broker <dir> --parent <pid>
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

from src import docx_render
from src.docx_render import PdfResult

ENV = "PDF_BROKER_DIR"
PID_FILE = "broker.pid"
WAIT_SECONDS = 600     # queue wait included: conversions take turns at Word
POLL_SECONDS = 0.2


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _broker_alive(queue: Path) -> bool:
    try:
        return _alive(int((queue / PID_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False


def _failed(detail: str) -> PdfResult:
    return PdfResult(status="failed", path="", pages=0, detail=detail)


def request(queue: Path, docx_path: Path, token: str, wait: float = WAIT_SECONDS) -> PdfResult:
    """Queue one conversion and wait for the helper's result."""
    docx_path.with_suffix(".pdf").unlink(missing_ok=True)
    rid = uuid.uuid4().hex
    req, res = queue / f"{rid}.req", queue / f"{rid}.res"
    _write(req, json.dumps({"docx": str(docx_path.resolve()), "token": token}))
    deadline = time.monotonic() + wait
    while True:
        if res.is_file():
            out = json.loads(res.read_text(encoding="utf-8"))
            res.unlink(missing_ok=True)
            return out
        gone = not _broker_alive(queue)
        if gone or time.monotonic() > deadline:
            req.unlink(missing_ok=True)
            if res.is_file():
                continue
            return _failed("pdf helper is not running" if gone
                           else f"pdf helper gave no result in {wait:.0f}s")
        time.sleep(POLL_SECONDS)


def handle(work: Path) -> None:
    """Convert one claimed request and write its result beside it."""
    try:
        job = json.loads(work.read_text(encoding="utf-8"))
        result = docx_render.convert_locally(Path(job["docx"]), job["token"])
    except Exception as exc:  # noqa: BLE001 - one bad request must not stop the helper
        result = _failed(f"pdf helper: {exc}")
    _write(work.with_suffix(".res"), json.dumps(result))
    work.unlink(missing_ok=True)


def serve(queue: Path, parent: int) -> None:
    """Take requests one at a time until the parent process is gone."""
    queue.mkdir(parents=True, exist_ok=True)
    _write(queue / PID_FILE, str(os.getpid()))
    while _alive(parent):
        for req in sorted(queue.glob("*.req")):
            work = req.with_suffix(".work")
            try:
                os.replace(req, work)       # a request its client withdrew is gone
            except FileNotFoundError:
                continue
            handle(work)
        time.sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.pdf_broker")
    parser.add_argument("queue", type=Path)
    parser.add_argument("--parent", type=int, required=True)
    args = parser.parse_args(argv)
    serve(args.queue, args.parent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
