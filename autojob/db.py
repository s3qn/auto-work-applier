"""SQLite schema and query helpers.

All timestamps are stored as ISO 8601 UTC strings (e.g. 2026-09-28T12:00:00Z).
Every write goes through parameterized SQL, no string formatting into queries.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autojob.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    website TEXT,
    tldr TEXT,
    researched_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    company_id INTEGER REFERENCES companies(id),
    location TEXT,
    description TEXT,
    found_at TEXT NOT NULL,
    fit_score REAL,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'filtered_out', 'queued', 'applying', 'applied', 'needs_help', 'failed')),
    dedup_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    channel TEXT NOT NULL
        CHECK (channel IN ('comeet', 'greenhouse', 'email', 'google_form', 'linkedin', 'other')),
    applied_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent'
        CHECK (status IN ('sent', 'replied', 'interview', 'rejected', 'not_interested')),
    notes TEXT
);

CREATE TABLE IF NOT EXISTS proofs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    kind TEXT NOT NULL CHECK (kind IN ('screenshot', 'confirmation_email', 'sent_email', 'pdf')),
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS help_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    trigger TEXT NOT NULL CHECK (trigger IN ('scheduled', 'manual')),
    found INTEGER NOT NULL DEFAULT 0,
    filtered INTEGER NOT NULL DEFAULT 0,
    applied INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    log TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_found_at ON jobs(found_at);
CREATE INDEX IF NOT EXISTS idx_applications_job_id ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_proofs_application_id ON proofs(application_id);
CREATE INDEX IF NOT EXISTS idx_help_items_job_id ON help_items(job_id);
CREATE INDEX IF NOT EXISTS idx_help_items_resolved_at ON help_items(resolved_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_dedup_key(company_name: str, title: str) -> str:
    raw = f"{company_name} {title}".lower()
    collapsed = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return collapsed


def get_connection(config: Config) -> sqlite3.Connection:
    config.ensure_dirs()
    # check_same_thread=False: FastAPI resolves a sync dependency like ours in a
    # worker thread, but an async route handler then runs on the event loop
    # thread. Each connection is still only ever used by the one request that
    # opened it, so relaxing the same-thread check is safe here.
    conn = sqlite3.connect(config.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(config: Config) -> None:
    conn = get_connection(config)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        migrate_schema(conn)
    finally:
        conn.close()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the original schema, safe to run on an
    existing database: every ALTER TABLE is guarded by a PRAGMA table_info
    check so it never runs twice."""
    if not _column_exists(conn, "jobs", "reason"):
        conn.execute("ALTER TABLE jobs ADD COLUMN reason TEXT")
    if not _column_exists(conn, "jobs", "external_id"):
        conn.execute("ALTER TABLE jobs ADD COLUMN external_id TEXT")
    if not _column_exists(conn, "companies", "careers_url"):
        conn.execute("ALTER TABLE companies ADD COLUMN careers_url TEXT")
    conn.commit()


# --- settings -----------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


# --- companies ------------------------------------------------------------

def get_or_create_company(conn: sqlite3.Connection, name: str, website: str | None = None) -> int:
    row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO companies (name, website) VALUES (?, ?)",
        (name, website),
    )
    conn.commit()
    return cur.lastrowid


def get_company(conn: sqlite3.Connection, company_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()


def get_company_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM companies WHERE name = ?", (name,)).fetchone()


def update_company_research(
    conn: sqlite3.Connection,
    company_id: int,
    *,
    website: str | None,
    tldr: str,
    careers_url: str | None,
) -> None:
    conn.execute(
        "UPDATE companies SET website = ?, tldr = ?, careers_url = ?, researched_at = ? WHERE id = ?",
        (website, tldr, careers_url, now_iso(), company_id),
    )
    conn.commit()


# --- jobs -------------------------------------------------------------

def create_job(
    conn: sqlite3.Connection,
    *,
    source: str,
    url: str,
    title: str,
    company_id: int | None = None,
    location: str | None = None,
    description: str | None = None,
    status: str = "new",
    fit_score: float | None = None,
    reason: str | None = None,
    external_id: str | None = None,
    dedup_key: str | None = None,
) -> int | None:
    """Insert a job. Returns the new job id, or None if the dedup key already exists."""
    key = dedup_key or normalize_dedup_key(str(company_id or ""), title)
    try:
        cur = conn.execute(
            "INSERT INTO jobs (source, url, title, company_id, location, description, "
            "found_at, fit_score, status, dedup_key, reason, external_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source, url, title, company_id, location, description,
                now_iso(), fit_score, status, key, reason, external_id,
            ),
        )
    except sqlite3.IntegrityError:
        return None
    conn.commit()
    return cur.lastrowid


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def get_job_with_company(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT jobs.*, companies.name AS company_name, companies.website AS company_website,
               companies.tldr AS company_tldr, companies.careers_url AS company_careers_url
        FROM jobs
        LEFT JOIN companies ON companies.id = jobs.company_id
        WHERE jobs.id = ?
        """,
        (job_id,),
    ).fetchone()


def job_url_exists(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
    return row is not None


def job_external_id_exists(conn: sqlite3.Connection, source: str, external_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM jobs WHERE source = ? AND external_id = ?", (source, external_id)
    ).fetchone()
    return row is not None


def list_jobs(conn: sqlite3.Connection, *, source: str | None = None, status: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM jobs WHERE 1=1"
    params: list = []
    if source is not None:
        query += " AND source = ?"
        params.append(source)
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY found_at DESC"
    return conn.execute(query, params).fetchall()


def list_jobs_with_company(conn: sqlite3.Connection, *, status: str | None = None) -> list[sqlite3.Row]:
    query = (
        "SELECT jobs.*, companies.name AS company_name, companies.tldr AS company_tldr, "
        "companies.website AS company_website, companies.careers_url AS company_careers_url "
        "FROM jobs LEFT JOIN companies ON companies.id = jobs.company_id WHERE 1=1"
    )
    params: list = []
    if status is not None:
        query += " AND jobs.status = ?"
        params.append(status)
    query += " ORDER BY jobs.found_at DESC"
    return conn.execute(query, params).fetchall()


def update_job_status(conn: sqlite3.Connection, job_id: int, status: str) -> None:
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()


def set_job_filter_result(
    conn: sqlite3.Connection, job_id: int, *, status: str, fit_score: float | None, reason: str
) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, fit_score = ?, reason = ? WHERE id = ?",
        (status, fit_score, reason, job_id),
    )
    conn.commit()


# --- applications -------------------------------------------------------

def create_application(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    channel: str,
    status: str = "sent",
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO applications (job_id, channel, applied_at, status, notes) VALUES (?, ?, ?, ?, ?)",
        (job_id, channel, now_iso(), status, notes),
    )
    conn.commit()
    return cur.lastrowid


def get_application(conn: sqlite3.Connection, application_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()


def list_applications_with_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT applications.*, jobs.title AS job_title, jobs.url AS job_url,
               companies.name AS company_name,
               (SELECT COUNT(*) FROM proofs WHERE proofs.application_id = applications.id) AS proof_count
        FROM applications
        JOIN jobs ON jobs.id = applications.job_id
        LEFT JOIN companies ON companies.id = jobs.company_id
        ORDER BY applications.applied_at DESC
        """
    ).fetchall()


def get_application_with_job(conn: sqlite3.Connection, application_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT applications.*, jobs.title AS job_title, jobs.url AS job_url,
               jobs.id AS job_id, companies.name AS company_name, companies.tldr AS company_tldr
        FROM applications
        JOIN jobs ON jobs.id = applications.job_id
        LEFT JOIN companies ON companies.id = jobs.company_id
        WHERE applications.id = ?
        """,
        (application_id,),
    ).fetchone()


def update_application(
    conn: sqlite3.Connection,
    application_id: int,
    *,
    status: str | None = None,
    notes: str | None = None,
) -> None:
    if status is not None:
        conn.execute("UPDATE applications SET status = ? WHERE id = ?", (status, application_id))
    if notes is not None:
        conn.execute("UPDATE applications SET notes = ? WHERE id = ?", (notes, application_id))
    conn.commit()


# --- proofs -------------------------------------------------------------

def create_proof(conn: sqlite3.Connection, *, application_id: int, kind: str, path: str) -> int:
    cur = conn.execute(
        "INSERT INTO proofs (application_id, kind, path, created_at) VALUES (?, ?, ?, ?)",
        (application_id, kind, path, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def list_proofs(conn: sqlite3.Connection, application_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM proofs WHERE application_id = ? ORDER BY created_at", (application_id,)
    ).fetchall()


# --- help items -----------------------------------------------------------

def create_help_item(conn: sqlite3.Connection, *, job_id: int, reason: str) -> int:
    cur = conn.execute(
        "INSERT INTO help_items (job_id, reason, created_at) VALUES (?, ?, ?)",
        (job_id, reason, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def list_open_help_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT help_items.*, jobs.title AS job_title, jobs.url AS job_url,
               companies.name AS company_name
        FROM help_items
        JOIN jobs ON jobs.id = help_items.job_id
        LEFT JOIN companies ON companies.id = jobs.company_id
        WHERE help_items.resolved_at IS NULL
        ORDER BY help_items.created_at
        """
    ).fetchall()


def get_help_item(conn: sqlite3.Connection, help_item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM help_items WHERE id = ?", (help_item_id,)).fetchone()


def resolve_help_item(conn: sqlite3.Connection, help_item_id: int, resolution: str) -> None:
    conn.execute(
        "UPDATE help_items SET resolved_at = ?, resolution = ? WHERE id = ?",
        (now_iso(), resolution, help_item_id),
    )
    conn.commit()


# --- runs -------------------------------------------------------------

def create_run(conn: sqlite3.Connection, *, trigger: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, trigger) VALUES (?, ?)",
        (now_iso(), trigger),
    )
    conn.commit()
    return cur.lastrowid


def update_run_log(conn: sqlite3.Connection, run_id: int, log: str) -> None:
    """Persist the log so far for a run still in progress, so the run detail
    page can poll and show live progress."""
    conn.execute("UPDATE runs SET log = ? WHERE id = ?", (log, run_id))
    conn.commit()


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    found: int = 0,
    filtered: int = 0,
    applied: int = 0,
    failed: int = 0,
    log: str = "",
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, found = ?, filtered = ?, applied = ?, failed = ?, log = ? "
        "WHERE id = ?",
        (now_iso(), found, filtered, applied, failed, log, run_id),
    )
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def list_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()


def run_in_progress(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT id FROM runs WHERE finished_at IS NULL LIMIT 1").fetchone()
    return row is not None


# --- dashboard stats ------------------------------------------------------

def get_dashboard_stats(conn: sqlite3.Connection) -> dict:
    today = now_iso()[:10]
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    applied_today = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE substr(applied_at, 1, 10) = ?", (today,)
    ).fetchone()[0]
    found_today = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE substr(found_at, 1, 10) = ?", (today,)
    ).fetchone()[0]
    replies_7d = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status = 'replied' AND applied_at >= ?",
        (cutoff_7d,),
    ).fetchone()[0]
    interviews = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status = 'interview'"
    ).fetchone()[0]
    return {
        "applied_today": applied_today,
        "found_today": found_today,
        "replies_7d": replies_7d,
        "interviews": interviews,
    }
