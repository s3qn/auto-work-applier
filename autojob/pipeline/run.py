"""Scan pipeline entry point.

This foundation step has no scraping or applying wired up yet. Running a
scan just records that a run happened, so the dashboard and the future
timer have something real to call.
"""

from __future__ import annotations

from autojob import db
from autojob.config import load_config


def run_scan(trigger: str = "manual") -> int:
    """Run one scan cycle and record it. Returns the new run id."""
    config = load_config()
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger=trigger)
        db.finish_run(
            conn,
            run_id,
            found=0,
            filtered=0,
            applied=0,
            failed=0,
            log="No sources connected yet.",
        )
        return run_id
    finally:
        conn.close()


if __name__ == "__main__":
    run_scan(trigger="scheduled")
