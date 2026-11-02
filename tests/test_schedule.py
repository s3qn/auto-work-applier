"""The fixed daily run time and its tie to the systemd timer."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from autojob.pipeline import schedule

TIMER = Path(__file__).resolve().parent.parent / "deploy" / "autojob-scan.timer"


def test_describe_next_run_before_and_after_the_run_time():
    before = datetime(2026, 11, 2, 7, 59, tzinfo=schedule.TIMEZONE)
    after = datetime(2026, 11, 2, 8, 1, tzinfo=schedule.TIMEZONE)
    assert schedule.describe_next_run(before) == "today at 08:00"
    assert schedule.describe_next_run(after) == "tomorrow at 08:00"
    assert schedule.next_scheduled_run(after).day == 3


def test_timer_file_matches_daily_run_time():
    lines = [l for l in TIMER.read_text().splitlines() if l.startswith("OnCalendar=")]
    assert lines == [f"OnCalendar=*-*-* {schedule.DAILY_RUN_TIME.strftime('%H:%M:%S')} Asia/Jerusalem"]
