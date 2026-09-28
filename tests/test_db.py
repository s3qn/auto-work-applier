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


def test_migrate_schema_adds_columns_to_legacy_db(config):
    """Simulates an existing database created before the reason,
    external_id and careers_url columns existed."""
    conn = db.get_connection(config)
    try:
        conn.executescript(db.SCHEMA)
        conn.commit()
        assert not db._column_exists(conn, "jobs", "reason")

        db.migrate_schema(conn)

        assert db._column_exists(conn, "jobs", "reason")
        assert db._column_exists(conn, "jobs", "external_id")
        assert db._column_exists(conn, "companies", "careers_url")

        # Running it again must not fail or duplicate anything.
        db.migrate_schema(conn)
    finally:
        conn.close()


def test_create_job_stores_external_id_and_reason(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        job_id = db.create_job(
            conn,
            source="linkedin",
            url="https://example.com/job/1",
            title="Pentester",
            external_id="12345",
            reason="good fit",
            status="queued",
            fit_score=7.5,
        )
        job = db.get_job(conn, job_id)
        assert job["external_id"] == "12345"
        assert job["reason"] == "good fit"
        assert job["status"] == "queued"
        assert job["fit_score"] == 7.5
    finally:
        conn.close()


def test_job_url_and_external_id_exist_helpers(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        db.create_job(
            conn, source="linkedin", url="https://example.com/job/1", title="Pentester", external_id="999"
        )
        assert db.job_url_exists(conn, "https://example.com/job/1") is True
        assert db.job_url_exists(conn, "https://example.com/job/2") is False
        assert db.job_external_id_exists(conn, "linkedin", "999") is True
        assert db.job_external_id_exists(conn, "linkedin", "888") is False
        assert db.job_external_id_exists(conn, "alljobs", "999") is False
    finally:
        conn.close()


def test_set_job_filter_result_updates_status_score_and_reason(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        job_id = db.create_job(conn, source="manual", url="https://example.com/x", title="Pentester")
        db.set_job_filter_result(conn, job_id, status="filtered_out", fit_score=2.0, reason="too senior")
        job = db.get_job(conn, job_id)
        assert job["status"] == "filtered_out"
        assert job["fit_score"] == 2.0
        assert job["reason"] == "too senior"
    finally:
        conn.close()


def test_update_company_research_sets_fields(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        db.update_company_research(
            conn, company_id, website="https://acme.example", tldr="Does security.", careers_url="https://acme.example/careers"
        )
        company = db.get_company(conn, company_id)
        assert company["website"] == "https://acme.example"
        assert company["tldr"] == "Does security."
        assert company["careers_url"] == "https://acme.example/careers"
        assert company["researched_at"] is not None
    finally:
        conn.close()


def test_list_jobs_with_company_joins_company_name(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        db.create_job(
            conn, source="manual", url="https://example.com/x", title="Pentester",
            company_id=company_id, status="queued",
        )
        rows = db.list_jobs_with_company(conn, status="queued")
        assert len(rows) == 1
        assert rows[0]["company_name"] == "Acme"
        assert db.list_jobs_with_company(conn, status="filtered_out") == []
    finally:
        conn.close()
