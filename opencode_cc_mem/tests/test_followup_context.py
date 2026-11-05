"""Follow-up routing and bounded wake-up context packages.

Run from opencode_cc_mem/:
    python3 -m pytest tests/test_followup_context.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router_runner import agentd


def make_engine(tmp_path):
    return agentd.ControlEngine(
        agentd.ControlStore(str(tmp_path / "agentd.db")),
        agentd.default_policy(),
        agentd.CompositeAdapter(out_root=tmp_path / "runs"),
    )


def approved(engine):
    req = agentd.ingest_letter(engine, agentd.NK2R_LETTER)
    engine.approve(req["work_item_id"], req["nonce"], agentd.operator())
    return req["work_item_id"]


def test_followup_on_active_item_is_context_post_only(tmp_path):
    engine = make_engine(tmp_path)
    wi = approved(engine)
    result = engine.follow_up(wi, agentd.operator(), "note: prefer s20000 later")
    assert result["kind"] == "post"
    assert len(engine.store.all("work_items")) == 1
    posts = engine.store.all("thread_posts", "work_item_id = ?", (wi,))
    assert len(posts) == 1 and posts[0]["delivered"] == 0


def test_followup_after_completion_creates_linked_work_item(tmp_path):
    engine = make_engine(tmp_path)
    wi = approved(engine)
    engine.execute(wi)
    result = engine.follow_up(wi, agentd.operator(), "now run selectivity")
    assert result["kind"] == "work_item"
    child = engine.store.get("work_items", "work_item_id", result["work_item_id"])
    parent = engine.store.get("work_items", "work_item_id", wi)
    assert child["parent_work_item_id"] == wi
    assert child["thread_id"] == parent["thread_id"]
    assert child["state"] == "candidate"
    assert parent["state"] == "completed"  # never resurrected
    assert len(engine.store.all("work_items")) == 2
    # The new item has its own single-use approval path.
    engine.approve(child["work_item_id"], result["nonce"], agentd.operator())
    assert engine.store.get("work_items", "work_item_id", child["work_item_id"])["state"] == "queued"


def test_context_package_is_bounded_and_marks_delta(tmp_path):
    engine = make_engine(tmp_path)
    wi = approved(engine)
    for i in range(3):
        engine.follow_up(wi, agentd.operator(), f"note {i}")
    engine.execute(wi)

    package = engine.context_package(wi, k=3)
    assert len(package["latest_attempts"]) == 1  # only one attempt exists
    assert len(package["delta"]["new_posts"]) == 3
    assert package["delta"]["current_state"] == "completed"
    assert package["mandate"]["mandate_id"]
    kinds = {l["kind"] for l in package["evidence_index"]}
    assert kinds == {"run_record", "gate_report", "ranked_candidates"}

    # Delta is consumed: a second package has no new posts.
    package2 = engine.context_package(wi, k=3)
    assert package2["delta"]["new_posts"] == []


def test_context_package_caps_attempts_and_links_parent(tmp_path):
    engine = make_engine(tmp_path)
    wi = approved(engine)
    engine.execute(wi)
    follow = engine.follow_up(wi, agentd.operator(), "second round")
    engine.approve(follow["work_item_id"], follow["nonce"], agentd.operator())
    engine.execute(follow["work_item_id"])

    package = engine.context_package(follow["work_item_id"], k=1)
    assert len(package["latest_attempts"]) == 1
    assert package["latest_attempts"][0]["key"].endswith(":1")
    assert package["parent_chain"] == [wi]
