"""Bridges the pipeline thread to the live SSE view.

`run_scan` (autojob/pipeline/run.py) runs in a background thread and writes
one `run_events` row per granular action (a source searched, a job scored,
a company researched, an apply attempt...) through `emit()`. The SSE route
in `autojob/web/routes.py` runs on the asyncio event loop in a different
thread and polls `run_events` for rows newer than a cursor id, so the two
sides never share state directly, only the database.

`bump_progress()` is the one helper every phase uses to update the shared
progress snapshot (per-phase done/total plus running found/filtered/applied
counts): it reads the run's current snapshot, updates the fields given, and
emits a `progress` event carrying the whole snapshot, so a client redraws
every bar and stat tile from that one event instead of merging partial
updates itself. `emit()` also persists a `progress` event's detail onto the
run row itself (`runs.progress`), so a reload with no new events still
shows the latest numbers.
"""

from __future__ import annotations

import json
import sqlite3

from autojob import db

PHASES = ("scan", "filter", "research", "apply", "system")
KINDS = ("info", "score", "progress", "error")

DEFAULT_PROGRESS = {
    "scan": {"done": 0, "total": 0},
    "filter": {"done": 0, "total": 0},
    "research": {"done": 0, "total": 0},
    "apply": {"done": 0, "total": 0},
    "found": 0,
    "filtered": 0,
    "applied": 0,
}


def emit(
    conn: sqlite3.Connection,
    run_id: int,
    phase: str,
    kind: str,
    message: str,
    detail: dict | None = None,
) -> int:
    """Insert one `run_events` row and commit immediately, so a concurrent
    reader on another connection sees it right away. Returns the new row id.

    When `kind` is "progress", `detail` also gets persisted onto the run row
    (`runs.progress`) so the run detail and live pages show the latest
    counts even on a plain reload with no new events.
    """
    detail_json = json.dumps(detail) if detail is not None else None
    row_id = db.create_run_event(
        conn, run_id=run_id, phase=phase, kind=kind, message=message, detail_json=detail_json
    )
    if kind == "progress" and detail is not None:
        db.update_run_progress(conn, run_id, detail)
    return row_id


def read_progress(conn: sqlite3.Connection, run_id: int) -> dict:
    """The run's latest progress snapshot, or a fresh all-zero one if the
    run has none yet (a brand new run, or one from before this feature)."""
    run_row = db.get_run(conn, run_id)
    raw = run_row["progress"] if run_row is not None else None
    snapshot = json.loads(json.dumps(DEFAULT_PROGRESS))  # cheap deep copy
    if not raw:
        return snapshot
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return snapshot
    if isinstance(loaded, dict):
        snapshot.update(loaded)
    return snapshot


def bump_progress(
    conn: sqlite3.Connection,
    run_id: int,
    phase: str,
    message: str = "",
    *,
    done: int | None = None,
    total: int | None = None,
    **counts,
) -> dict:
    """Read-modify-write the run's progress snapshot and emit a `progress`
    event carrying the whole thing. `done`/`total` update the named phase's
    bar; any other keyword (`found`, `filtered`, `applied`) sets a top-level
    running count. Returns the updated snapshot.
    """
    snapshot = read_progress(conn, run_id)
    if done is not None or total is not None:
        bucket = dict(snapshot.get(phase, {"done": 0, "total": 0}))
        if done is not None:
            bucket["done"] = done
        if total is not None:
            bucket["total"] = total
        snapshot[phase] = bucket
    for key, value in counts.items():
        snapshot[key] = value
    emit(conn, run_id, phase, "progress", message or f"{phase} progress update.", detail=snapshot)
    return snapshot
