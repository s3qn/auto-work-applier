"""Schema creation and basic query helper tests."""

from __future__ import annotations

from autojob import db


def test_init_db_creates_all_tables(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    finally:
        conn.close()
    expected = {
        "companies",
        "jobs",
        "applications",
        "proofs",
        "help_items",
        "settings",
        "runs",
    }
    assert expected.issubset(tables)


def test_create_job_dedup_key_prevents_duplicates(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        first = db.create_job(conn, source="manual", url="https://example.com/a", title="Same Job")
        second = db.create_job(conn, source="manual", url="https://example.com/b", title="Same Job")
        assert first is not None
        assert second is None
    finally:
        conn.close()


def test_settings_round_trip(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        db.set_setting(conn, "targets", {"titles": ["a", "b"]})
        assert db.get_setting(conn, "targets") == {"titles": ["a", "b"]}
        assert db.get_setting(conn, "missing", "default") == "default"
    finally:
        conn.close()
