"""The fixed daily run time. deploy/autojob-scan.timer must match it."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

DAILY_RUN_TIME = time(8, 0)
# deploy/autojob-pilot.timer must match.
PILOT_RUN_TIME = time(2, 0)
TIMEZONE = ZoneInfo("Asia/Jerusalem")


def next_scheduled_run(now: datetime | None = None) -> datetime:
    now = datetime.now(TIMEZONE) if now is None else now.astimezone(TIMEZONE)
    today = datetime.combine(now.date(), DAILY_RUN_TIME, tzinfo=TIMEZONE)
    return today if today > now else today + timedelta(days=1)


def describe_next_run(now: datetime | None = None) -> str:
    now = datetime.now(TIMEZONE) if now is None else now.astimezone(TIMEZONE)
    day = "today" if next_scheduled_run(now).date() == now.date() else "tomorrow"
    return f"{day} at {DAILY_RUN_TIME.strftime('%H:%M')}"
