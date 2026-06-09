"""Tests for the per-day breakdown added to /api/sessions/stats (Capella).

`_sessions_daily_stats` is a pure read-only helper over a state.db
``sessions`` table, so it is exercised against a temp sqlite file; the
handler is then called directly (same idiom as
test_web_server_session_search.py) to check the envelope.
"""

import asyncio
import sqlite3
import time
from datetime import date, datetime, timedelta

from hermes_cli import web_server
from hermes_cli.web_server import _sessions_daily_stats

# Fixed "now": late morning, so day arithmetic never straddles midnight.
NOW = datetime.now().replace(hour=11, minute=0, second=0, microsecond=0).timestamp()


def _day_start(days_ago: int) -> float:
    """Local midnight + 1h, ``days_ago`` days before NOW."""
    day = date.fromtimestamp(NOW) - timedelta(days=days_ago)
    return time.mktime(day.timetuple()) + 3600


def make_state_db(tmp_path, rows):
    """Minimal sessions table with just the columns the helper reads."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0
        )
        """
    )
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db_path


def test_daily_stats_buckets_by_day_and_source(tmp_path):
    db = make_state_db(tmp_path, [
        ("s1", "tui", _day_start(0), 10, 3),
        ("s2", "TUI", _day_start(0), 4, 1),          # source classing is case-insensitive
        ("s3", "cli", _day_start(1), 6, 2),
        ("s4", "cron", _day_start(2), 2, 0),
        ("s5", "telegram", _day_start(2), 8, 5),      # unknown source -> other
    ])
    stats = _sessions_daily_stats(db, days=30, now=NOW)

    assert "error" not in stats
    assert stats["window_days"] == 30
    assert len(stats["days"]) == 30
    assert [d["date"] for d in stats["days"]] == sorted(d["date"] for d in stats["days"])

    today = stats["days"][-1]
    assert today["date"] == date.fromtimestamp(NOW).isoformat()
    assert today["sessions"] == 2
    assert today["messages"] == 14
    assert today["tool_calls"] == 4
    assert today["by_source"]["tui"] == {"sessions": 2, "messages": 14, "tool_calls": 4}

    yesterday = stats["days"][-2]
    assert yesterday["by_source"]["cli"]["sessions"] == 1
    assert yesterday["messages"] == 6

    two_ago = stats["days"][-3]
    assert two_ago["by_source"]["cron"] == {"sessions": 1, "messages": 2, "tool_calls": 0}
    assert two_ago["by_source"]["other"] == {"sessions": 1, "messages": 8, "tool_calls": 5}

    totals = stats["totals"]
    assert totals["sessions"] == 5
    assert totals["messages"] == 30
    assert totals["tool_calls"] == 11
    assert totals["by_source"]["other"]["sessions"] == 1


def test_daily_stats_excludes_rows_outside_window(tmp_path):
    db = make_state_db(tmp_path, [
        ("old", "cli", NOW - 60 * 86400, 100, 50),
        ("new", "cli", _day_start(0), 1, 0),
    ])
    stats = _sessions_daily_stats(db, days=30, now=NOW)
    assert stats["totals"]["sessions"] == 1
    assert stats["totals"]["messages"] == 1


def test_daily_stats_null_counts_treated_as_zero(tmp_path):
    db = make_state_db(tmp_path, [("s1", "cli", _day_start(0), None, None)])
    stats = _sessions_daily_stats(db, days=7, now=NOW)
    assert stats["totals"] == {
        "sessions": 1,
        "messages": 0,
        "tool_calls": 0,
        "by_source": {
            "tui": {"sessions": 0, "messages": 0, "tool_calls": 0},
            "cli": {"sessions": 1, "messages": 0, "tool_calls": 0},
            "cron": {"sessions": 0, "messages": 0, "tool_calls": 0},
            "other": {"sessions": 0, "messages": 0, "tool_calls": 0},
        },
    }


def test_daily_stats_missing_db_reports_error(tmp_path):
    stats = _sessions_daily_stats(tmp_path / "absent.db", days=30, now=NOW)
    assert "error" in stats
    assert stats["totals"]["sessions"] == 0
    assert len(stats["days"]) == 30


def test_daily_stats_window_clamped(tmp_path):
    db = make_state_db(tmp_path, [("s1", "cli", _day_start(0), 1, 0)])
    assert _sessions_daily_stats(db, days=0, now=NOW)["window_days"] == 1
    assert _sessions_daily_stats(db, days=9999, now=NOW)["window_days"] == 365


def test_session_stats_handler_includes_daily():
    """Handler envelope: existing keys intact + new daily block."""
    result = asyncio.run(web_server.get_session_stats())
    assert {"total", "active_store", "archived", "messages", "by_source"} <= set(result)
    daily = result["daily"]
    assert daily["window_days"] == 30
    assert len(daily["days"]) == 30
