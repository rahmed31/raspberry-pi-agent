"""
tests/test_scheduler.py

Tests for the schedule parser — cron expression generation from
human-readable strings.
"""

import pytest
from agent.scheduler import parse_schedule


# ---------------------------------------------------------------------------
# Daily schedules
# ---------------------------------------------------------------------------

def test_daily_8am():
    assert parse_schedule("daily 8am") == "0 8 * * *"


def test_daily_midnight():
    assert parse_schedule("daily 12am") == "0 0 * * *"


def test_daily_noon():
    assert parse_schedule("daily 12pm") == "0 12 * * *"


def test_daily_6pm():
    assert parse_schedule("daily 6pm") == "0 18 * * *"


def test_daily_6_30pm():
    assert parse_schedule("daily 6:30pm") == "30 18 * * *"


def test_daily_9_15am():
    assert parse_schedule("daily 9:15am") == "15 9 * * *"


def test_daily_case_insensitive():
    assert parse_schedule("DAILY 8AM") == "0 8 * * *"


def test_daily_no_time_returns_none():
    assert parse_schedule("daily") is None


# ---------------------------------------------------------------------------
# Weekly schedules
# ---------------------------------------------------------------------------

def test_weekly_monday_9am():
    assert parse_schedule("weekly monday 9am") == "0 9 * * 1"


def test_weekly_mon_abbreviation():
    assert parse_schedule("weekly mon 9am") == "0 9 * * 1"


def test_weekly_friday_5pm():
    assert parse_schedule("weekly friday 5pm") == "0 17 * * 5"


def test_weekly_sunday():
    assert parse_schedule("weekly sunday 10am") == "0 10 * * 0"


def test_weekly_saturday():
    assert parse_schedule("weekly sat 8:30am") == "30 8 * * 6"


def test_weekly_no_day_returns_none():
    assert parse_schedule("weekly 9am") is None


def test_weekly_no_time_returns_none():
    assert parse_schedule("weekly monday") is None


# ---------------------------------------------------------------------------
# Invalid / unknown inputs
# ---------------------------------------------------------------------------

def test_empty_string_returns_none():
    assert parse_schedule("") is None


def test_unknown_frequency_returns_none():
    assert parse_schedule("hourly 5min") is None


def test_garbage_input_returns_none():
    assert parse_schedule("run it whenever") is None
