"""memory_filter: hold progress notes and procedures out of persistent memory."""

from __future__ import annotations

import time

from jermes.harness import Harness
from jermes.points import memory_filter as mf


def jev(fake, durable, progress, procedure):
    fake.on(lambda qid, q, s: {"type": "noul", "noul": {"durable": durable, "progress": progress,
                                                        "procedure": procedure}[qid]}
            if qid in ("durable", "progress", "procedure") else None)


def h(make_engine, mode="advise"):
    return Harness(make_engine(memory_filter=mode, risk_gate="off", skill_suggest="off"))


ADD = {"action": "add", "target": "memory", "content": "Pushed v0.5 as c4001bd; CI green."}


def test_writes_are_extracted_from_both_call_shapes():
    assert mf.writes_in(ADD) == [("memory", "add", ADD["content"])]
    batch = {"target": "user", "operations": [{"action": "remove", "old_text": "x"},
                                              {"action": "replace", "old_text": "a", "new_text": "Prefers Arial."}]}
    assert mf.writes_in(batch) == [("user", "replace", "Prefers Arial.")]
    assert mf.writes_in({"action": "remove", "old_text": "x"}) == []


def test_structure_check():
    assert mf.looks_structured("## Deploy\nrun it")
    assert mf.looks_structured("Steps: 1. link 2. set env 3. deploy")
    assert not mf.looks_structured("Vercel: token in .env; CLI needs PATH set; new projects default to SSO.")


def test_progress_note_held_once_then_goes_through(make_engine, fake):
    jev(fake, 0.2, 0.93, 0.1)
    hh = h(make_engine)
    d = hh.on_pre_tool_call(tool_name="memory", args=ADD, session_id="s")
    assert d["action"] == "block" and "task progress" in d["message"] and "retry" in d["message"]
    assert hh.on_pre_tool_call(tool_name="memory", args=ADD, session_id="s") is None


def test_durable_fact_passes(make_engine, fake):
    jev(fake, 0.85, 0.05, 0.3)
    assert h(make_engine).on_pre_tool_call(tool_name="memory", args={
        "action": "add", "content": "User's timezone is America/Los_Angeles."}, session_id="s") is None


def test_dense_fact_prose_is_not_held_as_procedure(make_engine, fake):
    jev(fake, 0.6, 0.2, 0.95)                        # Jev: "procedure-ish", but it's prose
    assert h(make_engine).on_pre_tool_call(tool_name="memory", args={
        "action": "add", "content": "Replicate: urllib gets 403, use curl; community models need a version hash."},
        session_id="s") is None


def test_stepwise_procedure_is_held(make_engine, fake):
    jev(fake, 0.5, 0.1, 0.97)
    d = h(make_engine).on_pre_tool_call(tool_name="memory", args={
        "action": "add", "content": "## Rate limiting\n1. track IP\n2. count per day\n3. return 429"}, session_id="s")
    assert d and "procedure" in d["message"] and "skill" in d["message"]


def test_remove_never_checked_and_shadow_never_blocks(make_engine, fake):
    assert h(make_engine).on_pre_tool_call(tool_name="memory", args={"action": "remove", "old_text": "x"},
                                           session_id="s") is None
    assert not fake.requests
    jev(fake, 0.2, 0.93, 0.1)
    hs = h(make_engine, mode="shadow")
    assert hs.on_pre_tool_call(tool_name="memory", args=ADD, session_id="s") is None
    for _ in range(50):
        if hs.engine.store.recent(5, point="memory_filter"):
            break
        time.sleep(0.05)
    row = hs.engine.store.recent(5, point="memory_filter")[0]
    assert row["action"] == "hold" and row["mode"] == "shadow"


def test_jev_error_fails_open(make_engine, fake):
    fake.status = 503
    assert h(make_engine).on_pre_tool_call(tool_name="memory", args=ADD, session_id="s") is None
