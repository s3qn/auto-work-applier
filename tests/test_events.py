"""Tests for the run_events table, its migration onto an existing database,
and the `autojob.pipeline.events` helper that bridges the pipeline thread
to the SSE live view."""

from __future__ import annotations

from autojob import db
from autojob.pipeline import events


def test_run_events_table_and_progress_column_exist_after_init_db(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        assert "run_events" in tables
        assert db._column_exists(conn, "runs", "progress")
    finally:
        conn.close()


def test_migrate_schema_adds_run_events_and_progress_to_a_legacy_db(config):
    """Simulates a database created before this feature existed: a `runs`
    table without the `progress` column and no `run_events` table at all."""
    conn = db.get_connection(config)
    try:
        conn.executescript(
            """
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                trigger TEXT NOT NULL,
                found INTEGER NOT NULL DEFAULT 0,
                filtered INTEGER NOT NULL DEFAULT 0,
                applied INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                log TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.commit()
        assert not db._column_exists(conn, "runs", "progress")

        # init_db's real sequence: the full CREATE TABLE IF NOT EXISTS
        # SCHEMA (a no-op on the existing `runs` table, but it does create
        # the brand new `run_events` table), then migrate_schema for the
        # column ALTER.
        conn.executescript(db.SCHEMA)
        conn.commit()
        db.migrate_schema(conn)

        assert db._column_exists(conn, "runs", "progress")
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        assert "run_events" in tables

        # Running it again (as a restart against the same database would)
        # must not fail or duplicate anything.
        conn.executescript(db.SCHEMA)
        db.migrate_schema(conn)
    finally:
        conn.close()


def test_create_run_event_and_list_since_returns_new_rows_in_order(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual")
        first = db.create_run_event(conn, run_id=run_id, phase="system", kind="info", message="a")
        second = db.create_run_event(conn, run_id=run_id, phase="scan", kind="info", message="b")
        third = db.create_run_event(
            conn, run_id=run_id, phase="filter", kind="score", message="c", detail_json='{"x": 1}'
        )

        all_rows = db.list_run_events_since(conn, run_id, 0)
        assert [r["id"] for r in all_rows] == [first, second, third]
        assert [r["message"] for r in all_rows] == ["a", "b", "c"]

        since_first = db.list_run_events_since(conn, run_id, first)
        assert [r["id"] for r in since_first] == [second, third]
        assert since_first[1]["detail"] == '{"x": 1}'
    finally:
        conn.close()


def test_update_run_counts_updates_only_the_given_fields(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual")
        db.update_run_counts(conn, run_id, found=5)
        run_row = db.get_run(conn, run_id)
        assert run_row["found"] == 5
        assert run_row["filtered"] == 0

        db.update_run_counts(conn, run_id, filtered=2, applied=1)
        run_row = db.get_run(conn, run_id)
        assert run_row["found"] == 5
        assert run_row["filtered"] == 2
        assert run_row["applied"] == 1
    finally:
        conn.close()


def test_update_run_progress_persists_json_on_the_run_row(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual")
        db.update_run_progress(conn, run_id, {"scan": {"done": 1, "total": 2}})
        run_row = db.get_run(conn, run_id)
        assert run_row["progress"] == '{"scan": {"done": 1, "total": 2}}'
    finally:
        conn.close()


# --- events helper ----------------------------------------------------

def test_emit_inserts_a_run_events_row(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual")
        row_id = events.emit(conn, run_id, "scan", "info", "hello", detail={"a": 1})
        rows = db.list_run_events_since(conn, run_id, 0)
        assert len(rows) == 1
        assert rows[0]["id"] == row_id
        assert rows[0]["message"] == "hello"
        assert rows[0]["detail"] == '{"a": 1}'
    finally:
        conn.close()


def test_emit_progress_persists_detail_onto_the_run_row(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual")
        events.emit(conn, run_id, "filter", "progress", "update", detail={"filter": {"done": 3, "total": 10}})
        run_row = db.get_run(conn, run_id)
        assert run_row["progress"] is not None
        assert '"done": 3' in run_row["progress"]
    finally:
        conn.close()


def test_emit_non_progress_event_does_not_touch_run_progress(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual")
        events.emit(conn, run_id, "scan", "info", "just a log line")
        run_row = db.get_run(conn, run_id)
        assert run_row["progress"] is None
    finally:
        conn.close()


def test_read_progress_defaults_to_a_zeroed_snapshot_when_none(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual")
        snapshot = events.read_progress(conn, run_id)
        assert snapshot == events.DEFAULT_PROGRESS
    finally:
        conn.close()


def test_bump_progress_reads_modifies_and_writes_the_snapshot(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual")
        events.bump_progress(conn, run_id, "scan", done=1, total=4)
        events.bump_progress(conn, run_id, "filter", done=2, total=10, filtered=1, found=4)

        snapshot = events.read_progress(conn, run_id)
        assert snapshot["scan"] == {"done": 1, "total": 4}
        assert snapshot["filter"] == {"done": 2, "total": 10}
        assert snapshot["filtered"] == 1
        assert snapshot["found"] == 4
        # A phase bump must not clobber the other phases' bars.
        assert snapshot["apply"] == {"done": 0, "total": 0}

        run_row = db.get_run(conn, run_id)
        assert run_row["progress"] is not None
    finally:
        conn.close()
