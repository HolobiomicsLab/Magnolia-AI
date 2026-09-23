"""Canonical run-id minting: one format for every writer (P1 uniform run_id).

Before this, the id was minted by three private copies — jobs.py,
ssh_slurm.py, and the memory CLI (which used a different format without the
hex suffix) — so the same run could be keyed differently depending on which
code path wrote it.
"""
import re
from datetime import datetime, timezone

from compchem_memory.runid import generate_run_id

_FORMAT = re.compile(r"^haddock3_(\d{8})_(\d{6})_[0-9a-f]{6}$")


def test_format_is_tool_timestamp_hex_utc():
    m = _FORMAT.fullmatch(generate_run_id("haddock3"))
    assert m, "expected <tool>_<YYYYMMDD>_<HHMMSS>_<6hex>"
    ts = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )
    assert abs((datetime.now(timezone.utc) - ts).total_seconds()) < 60


def test_missing_tool_falls_back_to_job():
    assert generate_run_id(None).startswith("job_")
    assert generate_run_id("").startswith("job_")


def test_ids_are_unique_within_the_same_second():
    ids = {generate_run_id("t") for _ in range(500)}
    assert len(ids) == 500
