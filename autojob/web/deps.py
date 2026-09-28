"""Shared FastAPI dependencies and small helpers for the web routes."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Iterator
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from starlette.templating import Jinja2Templates

from autojob import auth, db
from autojob.config import Config

JERUSALEM = ZoneInfo("Asia/Jerusalem")

DEFAULT_TARGETS = {
    "titles": [
        "Junior Penetration Tester",
        "Penetration Tester",
        "Red Team",
        "Security Researcher",
        "SOC Analyst",
        "Cyber Security Analyst",
        "Application Security",
        "Junior Security Engineer",
        "בודק חוסן",
        "בודק חדירות",
        "אנליסט סייבר",
    ],
    "locations": ["Israel"],
    "max_years": 3,
    "excluded_companies": [],
    "priority_companies": [],
    "daily_cap_per_source": 20,
    "scan_times": ["09:00", "16:00"],
}

PROFILE_FIELDS = [
    "full_name",
    "email",
    "phone",
    "city",
    "date_of_birth",
    "military_service",
    "driving_licence",
    "salary_expectation",
    "availability_notice",
    "willing_to_relocate",
    "work_mode_preference",
    "linkedin_url",
    "github_url",
    "htb_profile_url",
    "certifications",
    "languages",
    "about_me",
]

DEFAULT_PROFILE_ANSWERS = {field: "" for field in PROFILE_FIELDS}
DEFAULT_PROFILE_ANSWERS["certifications"] = "HTB CPTS"


def get_config(request: Request) -> Config:
    return request.app.state.config


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def get_limiter(request: Request) -> auth.LoginLimiter:
    return request.app.state.login_limiter


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.get_connection(get_config(request))
    try:
        yield conn
    finally:
        conn.close()


def get_targets(conn: sqlite3.Connection) -> dict:
    stored = db.get_setting(conn, "targets")
    merged = dict(DEFAULT_TARGETS)
    if stored:
        merged.update(stored)
    return merged


def get_profile_answers(conn: sqlite3.Connection) -> dict:
    stored = db.get_setting(conn, "profile.answers")
    merged = dict(DEFAULT_PROFILE_ANSWERS)
    if stored:
        merged.update(stored)
    return merged


def next_scan_display(conn: sqlite3.Connection) -> str:
    targets = get_targets(conn)
    scan_times = targets.get("scan_times") or DEFAULT_TARGETS["scan_times"]
    candidates: list[tuple[int, int]] = []
    for value in scan_times:
        try:
            hh, mm = value.split(":")
            candidates.append((int(hh), int(mm)))
        except ValueError:
            continue
    if not candidates:
        return "not scheduled"
    candidates.sort()
    now = datetime.now(JERUSALEM)
    for hh, mm in candidates:
        candidate_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate_dt > now:
            return f"{hh:02d}:{mm:02d}"
    hh, mm = candidates[0]
    return f"{hh:02d}:{mm:02d}"


def active_section(path: str) -> str:
    if path == "/" or path.startswith("/applications"):
        return "applications"
    if path.startswith("/jobs"):
        return "jobs"
    if path.startswith("/needs-help") or path.startswith("/help"):
        return "needs_help"
    if path.startswith("/submit"):
        return "submit"
    if path.startswith("/targets"):
        return "targets"
    if path.startswith("/runs"):
        return "runs"
    if path.startswith("/profile"):
        return "profile"
    return ""


def base_context(request: Request, conn: sqlite3.Connection) -> dict:
    return {
        "request": request,
        "csrf_token": auth.csrf_token_for_session(request),
        "needs_help_count": len(db.list_open_help_items(conn)),
        "next_scan": next_scan_display(conn),
        "active_section": active_section(request.url.path),
    }


def verify_csrf_or_403(request: Request, csrf_token: str) -> None:
    if not auth.verify_csrf(request, csrf_token):
        raise HTTPException(
            status_code=403,
            detail="Invalid session token. Reload the page and try again.",
        )
