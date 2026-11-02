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
        CHECK (
            channel IN ('comeet', 'greenhouse', 'lever', 'email', 'google_form', 'linkedin', 'other')
            OR channel LIKE 'generic:%'
            OR channel LIKE 'email:%'
            OR channel LIKE 'agent:%'
            OR channel LIKE 'pc:%'
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
    apply_url TEXT,
    email_to TEXT,
    email_draft TEXT,
    run_id INTEGER,
    pilot_verdict TEXT,
    cv_version TEXT
);

CREATE TABLE IF NOT EXISTS proofs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    kind TEXT NOT NULL
        CHECK (kind IN ('screenshot', 'confirmation', 'confirmation_email', 'sent_email', 'pdf')),
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
    log TEXT NOT NULL DEFAULT '',
    progress TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('scan', 'filter', 'research', 'apply', 'system')),
    kind TEXT NOT NULL CHECK (kind IN ('info', 'score', 'progress', 'error')),
    message TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS apply_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_pulled_at TEXT
);

CREATE TABLE IF NOT EXISTS apply_batch_items (
    batch_id INTEGER NOT NULL REFERENCES apply_batches(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'submitted', 'blocked', 'failed', 'expired')),
    reason TEXT,
    result_at TEXT,
    PRIMARY KEY (batch_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_found_at ON jobs(found_at);
CREATE INDEX IF NOT EXISTS idx_applications_job_id ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_proofs_application_id ON proofs(application_id);
CREATE INDEX IF NOT EXISTS idx_help_items_job_id ON help_items(job_id);
CREATE INDEX IF NOT EXISTS idx_help_items_resolved_at ON help_items(resolved_at);
CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id, id);
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


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row["sql"] if row else ""


_APPLICATIONS_COLUMNS = (
    "id", "job_id", "channel", "applied_at", "status", "notes",
    "cover_letter", "answers_json", "cv_path", "apply_url", "email_to", "email_draft",
    "run_id", "pilot_verdict", "cv_version",
)
_PROOFS_COLUMNS = ("id", "application_id", "kind", "path", "created_at")


def _schema_create_sql(table: str) -> str:
    """The `CREATE TABLE IF NOT EXISTS <table> (...)` statement for `table`
    out of SCHEMA, so a rebuild always matches the current definition."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", SCHEMA, flags=re.DOTALL
    )
    if match is None:
        raise ValueError(f"no CREATE TABLE for {table} in SCHEMA")
    return match.group(0)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(_table_sql(conn, table))


def _copy_rows(conn: sqlite3.Connection, source: str, target: str, columns: tuple[str, ...]) -> None:
    source_columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({source})")}
    shared = [c for c in columns if c in source_columns]
    conn.execute(
        f"INSERT INTO {target} ({', '.join(shared)}) SELECT {', '.join(shared)} FROM {source}"
    )


def _replace_table(conn: sqlite3.Connection, table: str, create_sql: str, columns: tuple[str, ...]) -> None:
    """Swap `table` for a fresh one built from `create_sql`, keeping every
    row. Builds `<table>_new`, copies, drops the old table, then renames the
    new one into place. Never renames the live table away: SQLite rewrites
    other tables' foreign keys to follow a rename, which is how `proofs`
    once ended up pointing at "applications_old". Must run with foreign
    keys off (see `migrate_schema`), since dropping a referenced table
    would otherwise fail on every child row."""
    new_name = f"{table}_new"
    conn.execute(f"DROP TABLE IF EXISTS {new_name}")
    conn.execute(create_sql.replace(f"CREATE TABLE IF NOT EXISTS {table} (", f"CREATE TABLE {new_name} (", 1))
    _copy_rows(conn, table, new_name, columns)
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {new_name} RENAME TO {table}")


def _recover_applications_old(conn: sqlite3.Connection) -> None:
    """An earlier rename-based rebuild could die after renaming
    `applications` to `applications_old`, leaving an empty `applications`
    and every row stranded in the old table. Move them back, then drop the
    leftover. If both tables hold rows, leave `applications_old` alone
    rather than guess which copy is right."""
    if not _table_exists(conn, "applications_old"):
        return
    old_count = conn.execute("SELECT COUNT(*) FROM applications_old").fetchone()[0]
    new_count = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    if old_count and new_count:
        return
    if old_count:
        _copy_rows(conn, "applications_old", "applications", _APPLICATIONS_COLUMNS)
    conn.execute("DROP TABLE applications_old")


def _heal_applications_old_references(conn: sqlite3.Connection) -> None:
    """Rebuild any table whose foreign key still names "applications_old"
    so it points at `applications` again."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql LIKE '%applications_old%'"
    ).fetchall()
    for row in rows:
        name = row["name"]
        if name == "applications_old":
            continue
        if name == "proofs":
            _replace_table(conn, "proofs", _schema_create_sql("proofs"), _PROOFS_COLUMNS)
            continue
        fixed = re.sub(r'"?applications_old"?', "applications", row["sql"])
        fixed = re.sub(
            rf'^CREATE TABLE "?{re.escape(name)}"?', f"CREATE TABLE IF NOT EXISTS {name}", fixed, count=1
        )
        columns = tuple(r["name"] for r in conn.execute(f"PRAGMA table_info({name})"))
        _replace_table(conn, name, fixed, columns)


def _applications_is_current(conn: sqlite3.Connection) -> bool:
    sql = _table_sql(conn, "applications")
    return (
        "ready_to_submit" in sql and "'lever'" in sql and "generic:" in sql and "email:" in sql and "agent:" in sql
        and "pc:" in sql and "cv_version" in sql
    )


def _rebuild_tables(conn: sqlite3.Connection) -> None:
    """Bring the CHECK constraints on `applications` and `proofs` up to date
    (SQLite can't ALTER a CHECK in place) and repair what the earlier
    rename-based rebuild could leave behind. Each step is guarded by a
    check of the live schema, so a current database is left untouched.

    Runs in one transaction with foreign keys off: the pragma is a no-op
    inside a transaction, so it is switched before BEGIN and restored after
    COMMIT (or ROLLBACK)."""
    needs_work = (
        not _applications_is_current(conn)
        or "'confirmation'" not in _table_sql(conn, "proofs")
        or _table_exists(conn, "applications_old")
        or conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND sql LIKE '%applications_old%'"
        ).fetchone() is not None
    )
    if not needs_work:
        return

    conn.commit()
    foreign_keys_were_on = conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        try:
            if not _applications_is_current(conn):
                _replace_table(conn, "applications", _schema_create_sql("applications"), _APPLICATIONS_COLUMNS)
            _recover_applications_old(conn)
            if "'confirmation'" not in _table_sql(conn, "proofs"):
                _replace_table(conn, "proofs", _schema_create_sql("proofs"), _PROOFS_COLUMNS)
            _heal_applications_old_references(conn)
            for statement in re.findall(r"CREATE INDEX IF NOT EXISTS [^;]+;", SCHEMA):
                conn.execute(statement)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        if foreign_keys_were_on:
            conn.execute("PRAGMA foreign_keys = ON")


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Add columns and constraints introduced after the original schema,
    safe to run on an existing database: every ALTER TABLE or table rebuild
    is guarded by a check of the current state so it never runs twice."""
    if not _column_exists(conn, "jobs", "reason"):
        conn.execute("ALTER TABLE jobs ADD COLUMN reason TEXT")
    if not _column_exists(conn, "jobs", "external_id"):
        conn.execute("ALTER TABLE jobs ADD COLUMN external_id TEXT")
    if not _column_exists(conn, "companies", "careers_url"):
        conn.execute("ALTER TABLE companies ADD COLUMN careers_url TEXT")
    if not _column_exists(conn, "runs", "mode"):
        conn.execute("ALTER TABLE runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'full'")
    if not _column_exists(conn, "runs", "progress"):
        conn.execute("ALTER TABLE runs ADD COLUMN progress TEXT")
    if not _column_exists(conn, "companies", "general_cv_url"):
        conn.execute("ALTER TABLE companies ADD COLUMN general_cv_url TEXT")
    if not _column_exists(conn, "companies", "cv_email"):
        conn.execute("ALTER TABLE companies ADD COLUMN cv_email TEXT")
    if not _column_exists(conn, "companies", "cv_email_source_url"):
        conn.execute("ALTER TABLE companies ADD COLUMN cv_email_source_url TEXT")
    if not _column_exists(conn, "companies", "channels_researched_at"):
        conn.execute("ALTER TABLE companies ADD COLUMN channels_researched_at TEXT")
    if not _column_exists(conn, "jobs", "company_site_url"):
        conn.execute("ALTER TABLE jobs ADD COLUMN company_site_url TEXT")
    if not _column_exists(conn, "applications", "run_id"):
        conn.execute("ALTER TABLE applications ADD COLUMN run_id INTEGER")
    if not _column_exists(conn, "applications", "pilot_verdict"):
        conn.execute("ALTER TABLE applications ADD COLUMN pilot_verdict TEXT")
    if not _column_exists(conn, "applications", "cv_version"):
        conn.execute("ALTER TABLE applications ADD COLUMN cv_version TEXT")
    if not _column_exists(conn, "jobs", "pc_attempts"):
        conn.execute("ALTER TABLE jobs ADD COLUMN pc_attempts INTEGER NOT NULL DEFAULT 0")
    for column in ("worker_phase TEXT", "worker_lease_until TEXT", "worker_lease_token TEXT",
                   "worker_application_id INTEGER"):
        if not _column_exists(conn, "jobs", column.split()[0]):
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column}")
    conn.commit()
    _rebuild_tables(conn)


# --- settings -----------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def claim_setting_flag(conn: sqlite3.Connection, key: str) -> bool:
    """Atomically flip a boolean setting from true to false. True only for the one caller that did it."""
    cur = conn.execute(
        "UPDATE settings SET value = ? WHERE key = ? AND value = ?",
        (json.dumps(False), key, json.dumps(True)),
    )
    conn.commit()
    return cur.rowcount == 1


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
    general_cv_url: str | None = None,
    cv_email: str | None = None,
    cv_email_source_url: str | None = None,
) -> None:
    """Store the result of one research call. Stamps `channels_researched_at`
    on every call, even when the three direct-apply fields all came back
    null, so re-research is bounded to companies never looked at this way."""
    conn.execute(
        "UPDATE companies SET website = ?, tldr = ?, careers_url = ?, general_cv_url = ?, "
        "cv_email = ?, cv_email_source_url = ?, researched_at = ?, channels_researched_at = ? "
        "WHERE id = ?",
        (
            website, tldr, careers_url, general_cv_url, cv_email, cv_email_source_url,
            now_iso(), now_iso(), company_id,
        ),
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
               companies.tldr AS company_tldr, companies.careers_url AS company_careers_url,
               companies.general_cv_url AS company_general_cv_url,
               companies.cv_email AS company_cv_email,
               companies.cv_email_source_url AS company_cv_email_source_url,
               companies.channels_researched_at AS company_channels_researched_at
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
        "companies.website AS company_website, companies.careers_url AS company_careers_url, "
        "companies.general_cv_url AS company_general_cv_url, "
        "companies.cv_email AS company_cv_email, "
        "companies.cv_email_source_url AS company_cv_email_source_url, "
        "companies.channels_researched_at AS company_channels_researched_at "
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


def set_job_company_site_url(conn: sqlite3.Connection, job_id: int, url: str | None) -> None:
    conn.execute("UPDATE jobs SET company_site_url = ? WHERE id = ?", (url, job_id))
    conn.commit()


def list_queued_jobs_needing_research(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """One queued job per company whose `channels_researched_at` is still
    null, newest job per company first, capped at `limit`. Covers companies
    researched before the direct-apply fields existed (an old `tldr` but no
    stamp) as well as brand new ones."""
    return conn.execute(
        """
        SELECT jobs.id, jobs.url, jobs.title, jobs.company_id, companies.name AS company_name
        FROM jobs
        JOIN companies ON companies.id = jobs.company_id
        WHERE jobs.status = 'queued'
          AND companies.channels_researched_at IS NULL
          AND jobs.id = (
              SELECT j2.id FROM jobs j2
              WHERE j2.company_id = jobs.company_id AND j2.status = 'queued'
              ORDER BY j2.found_at DESC, j2.id DESC
              LIMIT 1
          )
        ORDER BY jobs.found_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


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
    cover_letter: str | None = None,
    answers_json: str | None = None,
    cv_path: str | None = None,
    apply_url: str | None = None,
    email_to: str | None = None,
    email_draft: str | None = None,
    run_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO applications "
        "(job_id, channel, applied_at, status, notes, cover_letter, answers_json, cv_path, apply_url, "
        "email_to, email_draft, run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id, channel, now_iso(), status, notes, cover_letter, answers_json, cv_path, apply_url,
            email_to, email_draft, run_id,
        ),
    )
    conn.commit()
    return cur.lastrowid


PILOT_VERDICTS = ("right", "wrong")


def latest_pilot_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM runs WHERE mode = 'pilot' ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def list_pilot_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs WHERE mode = 'pilot' ORDER BY id DESC").fetchall()


def list_pilot_applications(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.*, j.title AS job_title, c.name AS company_name
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        LEFT JOIN companies c ON c.id = j.company_id
        WHERE a.run_id = ?
        ORDER BY a.id
        """,
        (run_id,),
    ).fetchall()


def set_pilot_verdict(conn: sqlite3.Connection, application_id: int, verdict: str | None) -> None:
    if verdict is not None and verdict not in PILOT_VERDICTS:
        raise ValueError(f"Invalid pilot verdict: {verdict!r}")
    conn.execute("UPDATE applications SET pilot_verdict = ? WHERE id = ?", (verdict, application_id))
    conn.commit()


def channel_group(channel: str) -> str:
    """`generic:foo.com` groups as `generic`, `email:x.com` as `email`."""
    return channel.split(":", 1)[0]


def pilot_summary(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """Per channel group: right, wrong, unreviewed, total and the success
    percent among reviewed rows (None while nothing is reviewed)."""
    groups: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT channel, pilot_verdict FROM applications WHERE run_id = ?", (run_id,)
    ).fetchall():
        g = groups.setdefault(
            channel_group(row["channel"]),
            {"channel": channel_group(row["channel"]), "right": 0, "wrong": 0, "unreviewed": 0, "total": 0},
        )
        g["total"] += 1
        if row["pilot_verdict"] in PILOT_VERDICTS:
            g[row["pilot_verdict"]] += 1
        else:
            g["unreviewed"] += 1
    result = sorted(groups.values(), key=lambda g: g["channel"])
    for g in result:
        reviewed = g["right"] + g["wrong"]
        g["percent"] = round(100 * g["right"] / reviewed) if reviewed else None
    return result


def get_application(conn: sqlite3.Connection, application_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()


def list_applications_with_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT applications.*, jobs.title AS job_title, jobs.url AS job_url,
               companies.id AS company_id, companies.name AS company_name, companies.tldr AS company_tldr,
               (SELECT COUNT(*) FROM proofs WHERE proofs.application_id = applications.id) AS proof_count,
               EXISTS (SELECT 1 FROM runs WHERE runs.id = applications.run_id AND runs.mode = 'pilot') AS is_pilot,
               (jobs.worker_phase = 'submit'
                OR (jobs.worker_lease_token IS NOT NULL AND jobs.worker_lease_until >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
               ) AS submit_pending
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
               jobs.id AS job_id, companies.name AS company_name, companies.tldr AS company_tldr,
               EXISTS (SELECT 1 FROM runs WHERE runs.id = applications.run_id AND runs.mode = 'pilot') AS is_pilot,
               (jobs.worker_phase = 'submit'
                OR (jobs.worker_lease_token IS NOT NULL AND jobs.worker_lease_until >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
               ) AS submit_pending
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
    cover_letter: str | None = None,
    answers_json: str | None = None,
    cv_path: str | None = None,
    channel: str | None = None,
    apply_url: str | None = None,
    email_to: str | None = None,
    email_draft: str | None = None,
) -> None:
    if status is not None:
        conn.execute("UPDATE applications SET status = ? WHERE id = ?", (status, application_id))
    if notes is not None:
        conn.execute("UPDATE applications SET notes = ? WHERE id = ?", (notes, application_id))
    if cover_letter is not None:
        conn.execute(
            "UPDATE applications SET cover_letter = ? WHERE id = ?", (cover_letter, application_id)
        )
    if answers_json is not None:
        conn.execute(
            "UPDATE applications SET answers_json = ? WHERE id = ?", (answers_json, application_id)
        )
    if cv_path is not None:
        conn.execute("UPDATE applications SET cv_path = ? WHERE id = ?", (cv_path, application_id))
    if channel is not None:
        conn.execute("UPDATE applications SET channel = ? WHERE id = ?", (channel, application_id))
    if apply_url is not None:
        conn.execute("UPDATE applications SET apply_url = ? WHERE id = ?", (apply_url, application_id))
    if email_to is not None:
        conn.execute("UPDATE applications SET email_to = ? WHERE id = ?", (email_to, application_id))
    if email_draft is not None:
        conn.execute(
            "UPDATE applications SET email_draft = ? WHERE id = ?", (email_draft, application_id)
        )
    conn.commit()


def email_already_sent(conn: sqlite3.Connection, job_id: int, email: str) -> bool:
    """True if an email application for this job has already gone out to
    this address, so the channel chain does not send a second one."""
    row = conn.execute(
        "SELECT 1 FROM applications WHERE job_id = ? AND email_to = ? AND status = 'sent' LIMIT 1",
        (job_id, email),
    ).fetchone()
    return row is not None


def email_draft_held_for_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """True if this job already has an email application drafted and
    waiting for Release, so a second Apply does not hold a second draft
    that could go out alongside it."""
    row = conn.execute(
        "SELECT 1 FROM applications WHERE job_id = ? AND channel LIKE 'email:%' "
        "AND status = 'ready_to_submit' LIMIT 1",
        (job_id,),
    ).fetchone()
    return row is not None


def count_emails_sent_since(conn: sqlite3.Connection, since_iso: str) -> int:
    """Count applications with a `sent_email` proof created at or after
    `since_iso` (an ISO timestamp, normally local midnight), used to enforce
    the daily email cap regardless of which job or company they went to."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT application_id) FROM proofs WHERE kind = 'sent_email' AND created_at >= ?",
        (since_iso,),
    ).fetchone()
    return row[0]


def resolve_open_help_items_for_job(conn: sqlite3.Connection, job_id: int, resolution: str) -> None:
    """Resolve every still-open help item for a job in one call, used when a
    later release (e.g. sending a held email) succeeds after an earlier
    needs_help escalation for the same job."""
    conn.execute(
        "UPDATE help_items SET resolved_at = ?, resolution = ? WHERE job_id = ? AND resolved_at IS NULL",
        (now_iso(), resolution, job_id),
    )
    conn.commit()


def count_applications_since(conn: sqlite3.Connection, *, source: str, since_iso: str) -> int:
    """Count applications sent for jobs from `source` at or after `since_iso`
    (an ISO timestamp), used to enforce the daily cap per source."""
    row = conn.execute(
        """
        SELECT COUNT(*) FROM applications
        JOIN jobs ON jobs.id = applications.job_id
        WHERE jobs.source = ? AND applications.applied_at >= ?
        """,
        (source, since_iso),
    ).fetchone()
    return row[0]


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


def get_proof(conn: sqlite3.Connection, proof_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM proofs WHERE id = ?", (proof_id,)).fetchone()


# --- help items -----------------------------------------------------------

def create_help_item(conn: sqlite3.Connection, *, job_id: int, reason: str) -> int:
    cur = conn.execute(
        "INSERT INTO help_items (job_id, reason, created_at) VALUES (?, ?, ?)",
        (job_id, reason, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def find_open_help_item(conn: sqlite3.Connection, job_id: int, *, reason_prefix: str) -> sqlite3.Row | None:
    """The newest unresolved help item for `job_id` whose reason starts with
    `reason_prefix`, or None."""
    return conn.execute(
        "SELECT * FROM help_items WHERE job_id = ? AND resolved_at IS NULL AND reason LIKE ? "
        "ORDER BY id DESC LIMIT 1",
        (job_id, reason_prefix.replace("%", "") + "%"),
    ).fetchone()


def update_help_item_reason(conn: sqlite3.Connection, help_item_id: int, reason: str) -> None:
    conn.execute("UPDATE help_items SET reason = ? WHERE id = ?", (reason, help_item_id))
    conn.commit()


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

def create_run(conn: sqlite3.Connection, *, trigger: str, mode: str = "full") -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, trigger, mode) VALUES (?, ?, ?)",
        (now_iso(), trigger, mode),
    )
    conn.commit()
    return cur.lastrowid


def close_interrupted_runs(
    conn: sqlite3.Connection, *, closed_by_run_id: int, keep_ids: tuple[int, ...] = ()
) -> int:
    """Close run rows that never got a finished_at (the process died). Only
    call while holding the run lock, so no live run can be hit. `keep_ids`
    are runs left open on purpose (a pilot window the PC worker works through)."""
    now = now_iso()
    skip = (closed_by_run_id, *keep_ids)
    placeholders = ", ".join("?" for _ in skip)
    cur = conn.execute(
        "UPDATE runs SET finished_at = ?, "
        "log = COALESCE(log, '') || ? "
        f"WHERE finished_at IS NULL AND id NOT IN ({placeholders})",
        (
            now,
            f"\n[{now}] Interrupted: the process exited before this run finished. "
            f"Closed by run {closed_by_run_id}.",
            *skip,
        ),
    )
    conn.commit()
    return cur.rowcount


def requeue_interrupted_applying_jobs(conn: sqlite3.Connection) -> int:
    """Put jobs left in 'applying' by an interrupted run back in the queue,
    except those with an application that is legitimately waiting or done and
    those the PC worker holds under a live lease. Expired worker leases go
    through `expire_worker_leases` first, so their lease columns are cleared
    and a fill placeholder is failed."""
    expired = expire_worker_leases(conn, now_iso())
    cur = conn.execute(
        "UPDATE jobs SET status = 'queued' WHERE status = 'applying' "
        "AND worker_lease_until IS NULL AND id NOT IN ("
        "SELECT job_id FROM applications "
        "WHERE status IN ('ready_to_submit', 'sent', 'replied', 'interview'))",
    )
    conn.commit()
    return expired + cur.rowcount


# --- PC worker leases ---------------------------------------------------

def claim_job(
    conn: sqlite3.Connection, job_id: int, *, lease_until: str, lease_token: str, phase: str,
    application_id: int,
) -> bool:
    """Atomically take a queued job for the PC worker. False when another
    caller (the scheduled run, the dashboard, a second worker) got it first."""
    cur = conn.execute(
        "UPDATE jobs SET status = 'applying', worker_phase = ?, worker_lease_until = ?, "
        "worker_lease_token = ?, worker_application_id = ? WHERE id = ? AND status = 'queued'",
        (phase, lease_until, lease_token, application_id, job_id),
    )
    conn.commit()
    return cur.rowcount == 1


def extend_lease(conn: sqlite3.Connection, job_id: int, lease_token: str, lease_until: str) -> bool:
    cur = conn.execute(
        "UPDATE jobs SET worker_lease_until = ? WHERE id = ? AND worker_lease_token = ? "
        "AND status = 'applying' AND worker_lease_until >= ?",
        (lease_until, job_id, lease_token, now_iso()),
    )
    conn.commit()
    return cur.rowcount == 1


def clear_lease(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET worker_lease_until = NULL, worker_lease_token = NULL, "
        "worker_application_id = NULL, worker_phase = NULL WHERE id = ?",
        (job_id,),
    )
    conn.commit()


def expire_worker_leases(conn: sqlite3.Connection, now: str) -> int:
    """Return jobs whose worker lease ran out to the queue. The phase is
    kept so a submit session is retried as a submit session; a fill-phase
    placeholder application becomes `failed`."""
    rows = conn.execute(
        "SELECT id, worker_phase, worker_application_id FROM jobs "
        "WHERE status = 'applying' AND worker_lease_until IS NOT NULL AND worker_lease_until < ?",
        (now,),
    ).fetchall()
    for row in rows:
        if row["worker_phase"] == "fill" and row["worker_application_id"] is not None:
            conn.execute(
                "UPDATE applications SET status = 'failed', notes = ? WHERE id = ?",
                ("PC worker lease expired without a result.", row["worker_application_id"]),
            )
        conn.execute(
            "UPDATE jobs SET status = 'queued', worker_lease_until = NULL, worker_lease_token = NULL "
            "WHERE id = ?",
            (row["id"],),
        )
    conn.commit()
    return len(rows)


def release_lease(conn: sqlite3.Connection, job_id: int) -> None:
    """Back to the queue, phase kept, lease cleared."""
    conn.execute(
        "UPDATE jobs SET status = 'queued', worker_lease_until = NULL, worker_lease_token = NULL "
        "WHERE id = ?",
        (job_id,),
    )
    conn.commit()


def queue_worker_submit(conn: sqlite3.Connection, job_id: int, application_id: int) -> bool:
    """Queue a submit session for the PC worker. False (nothing changed)
    while a submit is already queued or the worker holds a live lease."""
    cur = conn.execute(
        "UPDATE jobs SET status = 'queued', worker_phase = 'submit', worker_application_id = ?, "
        "worker_lease_until = NULL, worker_lease_token = NULL WHERE id = ? "
        "AND COALESCE(worker_phase, '') != 'submit' "
        "AND (worker_lease_token IS NULL OR worker_lease_until IS NULL OR worker_lease_until < ?)",
        (application_id, job_id, now_iso()),
    )
    conn.commit()
    return cur.rowcount == 1


def delete_application(conn: sqlite3.Connection, application_id: int) -> None:
    conn.execute("DELETE FROM proofs WHERE application_id = ?", (application_id,))
    conn.execute("DELETE FROM applications WHERE id = ?", (application_id,))
    conn.commit()


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


def update_run_counts(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    found: int | None = None,
    filtered: int | None = None,
    applied: int | None = None,
    failed: int | None = None,
) -> None:
    """Update whichever of the running counts are given, live while a run is
    still in progress, so the stat tiles on the run detail page can update
    before the run finishes instead of only filling in at the end."""
    columns = []
    params: list = []
    for column, value in (("found", found), ("filtered", filtered), ("applied", applied), ("failed", failed)):
        if value is not None:
            columns.append(f"{column} = ?")
            params.append(value)
    if not columns:
        return
    params.append(run_id)
    conn.execute(f"UPDATE runs SET {', '.join(columns)} WHERE id = ?", params)
    conn.commit()


def update_run_progress(conn: sqlite3.Connection, run_id: int, progress: dict) -> None:
    """Persist the latest progress snapshot (per-phase done/total and
    running found/filtered/applied) onto the run row, so a reload without
    any new `run_events` rows still shows the current bars."""
    conn.execute("UPDATE runs SET progress = ? WHERE id = ?", (json.dumps(progress), run_id))
    conn.commit()


# --- run events (live activity feed) --------------------------------------

def create_run_event(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    phase: str,
    kind: str,
    message: str,
    detail_json: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO run_events (run_id, ts, phase, kind, message, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, now_iso(), phase, kind, message, detail_json),
    )
    conn.commit()
    return cur.lastrowid


def list_run_events_since(conn: sqlite3.Connection, run_id: int, since_id: int = 0) -> list[sqlite3.Row]:
    """Every `run_events` row for `run_id` with an id greater than
    `since_id`, oldest first: the SSE route's replay-then-follow query."""
    return conn.execute(
        "SELECT * FROM run_events WHERE run_id = ? AND id > ? ORDER BY id", (run_id, since_id)
    ).fetchall()


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


# --- daily apply queue (PC pull) ----------------------------------------

BATCH_DONE_STATUSES = ("sent", "replied", "interview", "rejected")


def jerusalem_day(moment: datetime | None = None) -> str:
    from zoneinfo import ZoneInfo

    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%d")


def open_batch_job_ids(conn: sqlite3.Connection) -> set[int]:
    """Jobs still open in today's batch only: a stale batch never holds jobs back."""
    rows = conn.execute(
        "SELECT apply_batch_items.job_id FROM apply_batch_items "
        "JOIN apply_batches ON apply_batches.id = apply_batch_items.batch_id "
        "WHERE apply_batch_items.state = 'open' AND apply_batches.day = ?",
        (jerusalem_day(),),
    ).fetchall()
    return {row["job_id"] for row in rows}


def select_apply_batch_jobs(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Queued jobs the PC apply session may take, best fit first."""
    now = now_iso()
    rows = conn.execute(
        "SELECT jobs.*, companies.name AS company_name FROM jobs "
        "LEFT JOIN companies ON companies.id = jobs.company_id "
        "WHERE jobs.status = 'queued' "
        "AND NOT (jobs.worker_lease_token IS NOT NULL AND jobs.worker_lease_until >= ?) "
        "AND COALESCE(jobs.worker_phase, '') != 'submit' "
        "AND jobs.id NOT IN (SELECT i.job_id FROM apply_batch_items i "
        "JOIN apply_batches b ON b.id = i.batch_id WHERE i.state = 'open' AND b.day = ?) "
        f"AND jobs.id NOT IN (SELECT job_id FROM applications WHERE status IN "
        f"({', '.join('?' for _ in BATCH_DONE_STATUSES)})) "
        "AND LOWER(COALESCE(jobs.reason, '')) NOT LIKE 'vip%' "
        "AND jobs.url NOT LIKE '%/User/Register/CreditCard.aspx%' "
        "AND jobs.url NOT LIKE '%/Register/%' "
        "ORDER BY jobs.fit_score DESC, jobs.found_at DESC, jobs.id DESC LIMIT ?",
        (now, jerusalem_day(), *BATCH_DONE_STATUSES, limit),
    ).fetchall()
    return rows


def expire_open_batch_items_before(conn: sqlite3.Connection, day: str) -> int:
    cur = conn.execute(
        "UPDATE apply_batch_items SET state = 'expired', result_at = ? WHERE state = 'open' "
        "AND batch_id IN (SELECT id FROM apply_batches WHERE day < ?)",
        (now_iso(), day),
    )
    conn.commit()
    return cur.rowcount


def get_batch_for_day(conn: sqlite3.Connection, day: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM apply_batches WHERE day = ? ORDER BY id LIMIT 1", (day,)).fetchone()


def create_batch(conn: sqlite3.Connection, day: str, items: list[tuple[int, str]]) -> int:
    """Store a batch with its frozen payloads: `items` is (job_id, payload_json)."""
    cur = conn.execute("INSERT INTO apply_batches (day, created_at) VALUES (?, ?)", (day, now_iso()))
    batch_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO apply_batch_items (batch_id, job_id, payload_json) VALUES (?, ?, ?)",
        [(batch_id, job_id, payload) for job_id, payload in items],
    )
    conn.commit()
    return batch_id


def touch_batch_pull(conn: sqlite3.Connection, batch_id: int) -> None:
    conn.execute("UPDATE apply_batches SET last_pulled_at = ? WHERE id = ?", (now_iso(), batch_id))
    conn.commit()


def list_batch_items(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT apply_batch_items.*, jobs.title AS title, jobs.fit_score AS fit_score, "
        "companies.name AS company FROM apply_batch_items "
        "JOIN jobs ON jobs.id = apply_batch_items.job_id "
        "LEFT JOIN companies ON companies.id = jobs.company_id "
        "WHERE batch_id = ? ORDER BY apply_batch_items.rowid",
        (batch_id,),
    ).fetchall()


def latest_batch_item(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM apply_batch_items WHERE job_id = ? ORDER BY batch_id DESC LIMIT 1", (job_id,)
    ).fetchone()


def record_batch_result(
    conn: sqlite3.Connection, batch_id: int, job_id: int, state: str, reason: str | None
) -> None:
    conn.execute(
        "UPDATE apply_batch_items SET state = ?, reason = ?, result_at = ? WHERE batch_id = ? AND job_id = ?",
        (state, reason, now_iso(), batch_id, job_id),
    )
    conn.commit()
