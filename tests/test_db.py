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

        assert not db._column_exists(conn, "companies", "channels_researched_at")
        assert not db._column_exists(conn, "jobs", "company_site_url")

        db.migrate_schema(conn)

        assert db._column_exists(conn, "jobs", "reason")
        assert db._column_exists(conn, "jobs", "external_id")
        assert db._column_exists(conn, "companies", "careers_url")
        assert db._column_exists(conn, "companies", "general_cv_url")
        assert db._column_exists(conn, "companies", "cv_email")
        assert db._column_exists(conn, "companies", "cv_email_source_url")
        assert db._column_exists(conn, "companies", "channels_researched_at")
        assert db._column_exists(conn, "jobs", "company_site_url")

        # Running it again must not fail or duplicate anything.
        db.migrate_schema(conn)
    finally:
        conn.close()


_PRE_CHANGE_APPLICATIONS_SQL = """
CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    channel TEXT NOT NULL
        CHECK (
            channel IN ('comeet', 'greenhouse', 'lever', 'email', 'google_form', 'linkedin', 'other')
            OR channel LIKE 'generic:%'
        ),
    applied_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent'
        CHECK (status IN (
            'ready_to_submit', 'sent', 'replied', 'interview', 'rejected',
            'not_interested', 'needs_help', 'failed', 'discarded'
        )),
    notes TEXT,
    cover_letter TEXT,
    answers_json TEXT,
    cv_path TEXT,
    apply_url TEXT
)
"""


def test_migrate_schema_rebuilds_applications_table_for_email_channel(config):
    """Simulates a database from before the `email:<domain>` channel existed:
    an applications table with the old CHECK constraint and no email
    columns, with one real row in it."""
    conn = db.get_connection(config)
    try:
        conn.executescript(db.SCHEMA)
        conn.commit()
        conn.execute("DROP TABLE applications")
        conn.execute(_PRE_CHANGE_APPLICATIONS_SQL)
        conn.commit()

        # Insert the job with raw SQL: at this point (before migrate_schema
        # runs) the jobs table is still the legacy shape, with no reason or
        # external_id columns, so db.create_job's INSERT would not match it.
        conn.execute(
            "INSERT INTO jobs (source, url, title, found_at, status, dedup_key) "
            "VALUES ('manual', 'https://example.com/old', 'Old Job', ?, 'new', 'dedup-old')",
            (db.now_iso(),),
        )
        conn.commit()
        job_id = conn.execute("SELECT id FROM jobs WHERE dedup_key = 'dedup-old'").fetchone()["id"]
        conn.execute(
            "INSERT INTO applications (job_id, channel, applied_at, status) VALUES (?, 'greenhouse', ?, 'sent')",
            (job_id, db.now_iso()),
        )
        conn.commit()

        db.migrate_schema(conn)
        db.migrate_schema(conn)  # must be idempotent, must not fail or duplicate anything

        assert db._column_exists(conn, "applications", "email_to")
        assert db._column_exists(conn, "applications", "email_draft")

        survivor = conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
        assert survivor is not None
        assert survivor["channel"] == "greenhouse"

        conn.execute(
            "INSERT INTO applications (job_id, channel, applied_at, status, email_to) "
            "VALUES (?, 'email:acme.com', ?, 'ready_to_submit', 'jobs@acme.com')",
            (job_id, db.now_iso()),
        )
        conn.commit()
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


def test_email_already_sent_checks_job_and_address(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        job_id = db.create_job(conn, source="manual", url="https://example.com/x", title="Pentester")
        assert db.email_already_sent(conn, job_id, "jobs@acme.example") is False

        app_id = db.create_application(
            conn, job_id=job_id, channel="email:acme.example", status="ready_to_submit",
            email_to="jobs@acme.example",
        )
        assert db.email_already_sent(conn, job_id, "jobs@acme.example") is False

        db.update_application(conn, app_id, status="sent")
        assert db.email_already_sent(conn, job_id, "jobs@acme.example") is True
        assert db.email_already_sent(conn, job_id, "someone-else@acme.example") is False
    finally:
        conn.close()


def test_count_emails_sent_since_counts_distinct_applications(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        job_id = db.create_job(conn, source="manual", url="https://example.com/x", title="Pentester")
        app_id = db.create_application(conn, job_id=job_id, channel="email:acme.example", status="sent")
        since = "2020-01-01T00:00:00Z"
        assert db.count_emails_sent_since(conn, since) == 0

        db.create_proof(conn, application_id=app_id, kind="sent_email", path="sent.eml")
        db.create_proof(conn, application_id=app_id, kind="sent_email", path="sent_email.txt")
        assert db.count_emails_sent_since(conn, since) == 1

        future = "2999-01-01T00:00:00Z"
        assert db.count_emails_sent_since(conn, future) == 0
    finally:
        conn.close()


def test_list_queued_jobs_needing_research_skips_stamped_companies(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        fresh_id = db.get_or_create_company(conn, "Fresh")
        db.create_job(
            conn, source="manual", url="https://example.com/fresh", title="Pentester",
            company_id=fresh_id, status="queued",
        )
        stamped_id = db.get_or_create_company(conn, "Stamped")
        db.update_company_research(
            conn, stamped_id, website=None, tldr="Already researched.", careers_url=None,
        )
        db.create_job(
            conn, source="manual", url="https://example.com/stamped", title="Pentester",
            company_id=stamped_id, status="queued",
        )
        not_queued_id = db.get_or_create_company(conn, "NotQueued")
        db.create_job(
            conn, source="manual", url="https://example.com/notqueued", title="Pentester",
            company_id=not_queued_id, status="new",
        )

        rows = db.list_queued_jobs_needing_research(conn, 10)
        company_names = {row["company_name"] for row in rows}
        assert company_names == {"Fresh"}
    finally:
        conn.close()


_OLDEST_APPLICATIONS_SQL = """
CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    channel TEXT NOT NULL CHECK (channel IN ('comeet', 'email', 'google_form', 'linkedin', 'other')),
    applied_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent'
        CHECK (status IN ('sent', 'replied', 'interview', 'rejected', 'not_interested')),
    notes TEXT
)
"""

_OLDEST_PROOFS_SQL = """
CREATE TABLE proofs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    kind TEXT NOT NULL CHECK (kind IN ('screenshot', 'confirmation_email', 'sent_email')),
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_BROKEN_PROOFS_SQL = """
CREATE TABLE proofs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES "applications_old"(id),
    kind TEXT NOT NULL
        CHECK (kind IN ('screenshot', 'confirmation', 'confirmation_email', 'sent_email', 'pdf')),
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def _seed_legacy_job(conn) -> int:
    conn.execute(
        "INSERT INTO jobs (source, url, title, found_at, status, dedup_key) "
        "VALUES ('manual', 'https://example.com/old', 'Old Job', ?, 'new', 'dedup-old')",
        (db.now_iso(),),
    )
    conn.commit()
    return conn.execute("SELECT id FROM jobs WHERE dedup_key = 'dedup-old'").fetchone()["id"]


def _no_table_references_applications_old(conn) -> bool:
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    return all("applications_old" not in (row["sql"] or "") for row in rows)


def test_migrate_schema_rebuild_keeps_proofs_pointing_at_applications(config):
    """An old-schema database with proof rows: the applications rebuild must
    not leave `proofs` referencing a renamed table, and must not lose rows."""
    conn = db.get_connection(config)
    try:
        conn.executescript(db.SCHEMA)
        conn.execute("DROP TABLE proofs")
        conn.execute("DROP TABLE applications")
        conn.execute(_OLDEST_APPLICATIONS_SQL)
        conn.execute(_OLDEST_PROOFS_SQL)
        conn.commit()
        job_id = _seed_legacy_job(conn)
        conn.execute(
            "INSERT INTO applications (job_id, channel, applied_at, status) VALUES (?, 'comeet', ?, 'sent')",
            (job_id, db.now_iso()),
        )
        app_id = conn.execute("SELECT id FROM applications").fetchone()["id"]
        conn.execute(
            "INSERT INTO proofs (application_id, kind, path, created_at) VALUES (?, 'screenshot', '/x.png', ?)",
            (app_id, db.now_iso()),
        )
        conn.commit()

        db.migrate_schema(conn)
        db.migrate_schema(conn)

        assert _no_table_references_applications_old(conn)
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM proofs").fetchone()[0] == 1
        assert "email:" in db._table_sql(conn, "applications")
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        db.create_proof(conn, application_id=app_id, kind="pdf", path="/y.pdf")
        assert len(db.list_proofs(conn, app_id)) == 2
    finally:
        conn.close()


def test_migrate_schema_heals_proofs_referencing_applications_old(config):
    """The state an earlier rename-based rebuild left behind: proofs
    references "applications_old", which no longer exists."""
    conn = db.get_connection(config)
    try:
        conn.executescript(db.SCHEMA)
        conn.execute("DROP TABLE proofs")
        conn.execute(_BROKEN_PROOFS_SQL)
        conn.commit()
        job_id = _seed_legacy_job(conn)
        conn.execute(
            "INSERT INTO applications (job_id, channel, applied_at, status) VALUES (?, 'greenhouse', ?, 'sent')",
            (job_id, db.now_iso()),
        )
        conn.commit()
        app_id = conn.execute("SELECT id FROM applications").fetchone()["id"]

        db.migrate_schema(conn)

        assert _no_table_references_applications_old(conn)
        db.create_proof(conn, application_id=app_id, kind="screenshot", path="/x.png")
        assert len(db.list_proofs(conn, app_id)) == 1
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_proofs_application_id'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_migrate_schema_recovers_rows_stranded_in_applications_old(config):
    """A rebuild that died after the rename: `applications` exists but is
    empty, every row is still in `applications_old`, proofs points there."""
    conn = db.get_connection(config)
    try:
        conn.executescript(db.SCHEMA)
        conn.execute("DROP TABLE proofs")
        conn.execute("ALTER TABLE applications RENAME TO applications_old")
        conn.execute(_PRE_CHANGE_APPLICATIONS_SQL)
        conn.execute(_BROKEN_PROOFS_SQL)
        conn.commit()
        job_id = _seed_legacy_job(conn)
        conn.execute(
            "INSERT INTO applications_old (job_id, channel, applied_at, status) VALUES (?, 'lever', ?, 'sent')",
            (job_id, db.now_iso()),
        )
        app_id = conn.execute("SELECT id FROM applications_old").fetchone()["id"]
        conn.execute(
            "INSERT INTO proofs (application_id, kind, path, created_at) VALUES (?, 'screenshot', '/x.png', ?)",
            (app_id, db.now_iso()),
        )
        conn.commit()

        db.migrate_schema(conn)

        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'applications_old'").fetchone() is None
        assert _no_table_references_applications_old(conn)
        survivor = db.get_application(conn, app_id)
        assert survivor is not None and survivor["channel"] == "lever"
        assert len(db.list_proofs(conn, app_id)) == 1
        db.create_proof(conn, application_id=app_id, kind="pdf", path="/y.pdf")
    finally:
        conn.close()
