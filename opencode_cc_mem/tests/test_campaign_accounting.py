import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign_runner.accounting import (  # noqa: E402
    elapsed_to_core_hours,
    read_session_usage,
)


def test_read_session_usage_sums_assistant_rows(tmp_path):
    db = tmp_path / "usage.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE session_message (session_id TEXT, type TEXT, data TEXT)"
    )
    connection.execute(
        "INSERT INTO session_message VALUES (?, ?, ?)",
        (
            "ses_1",
            "assistant",
            json.dumps(
                {
                    "cost": 0.25,
                    "tokens": {
                        "input": 10,
                        "output": 3,
                        "reasoning": 2,
                        "cache": {"read": 4, "write": 1},
                    },
                }
            ),
        ),
    )
    connection.execute(
        "INSERT INTO session_message VALUES (?, ?, ?)",
        (
            "ses_1",
            "assistant",
            json.dumps(
                {
                    "cost": 0.10,
                    "tokens": {
                        "input": 5,
                        "output": 1,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                }
            ),
        ),
    )
    connection.commit()
    connection.close()
    usage = read_session_usage(db, "ses_1")
    assert usage["input_tokens"] == 15
    assert usage["output_tokens"] == 4
    assert usage["reasoning_tokens"] == 2
    assert usage["cache_read_tokens"] == 4
    assert usage["cost_usd"] == 0.35

    empty = read_session_usage(tmp_path / "missing.db", "ses_1")
    assert empty["input_tokens"] == 0


def test_elapsed_to_core_hours():
    assert elapsed_to_core_hours("02:00:00", 4) == 8
    assert elapsed_to_core_hours("00:30:00", 2) == 1
    assert elapsed_to_core_hours("30:00", 2) == 1
    assert elapsed_to_core_hours("1-02:00:00", 4) == 104
    assert elapsed_to_core_hours("bad", 2) == 0
