"""tool_request middleware: decide once, hook reuses it; slow decisions fail open."""

import time

from jermes.harness import Harness


def test_hook_reuses_the_middleware_decision(make_engine):
    h = Harness(make_engine(risk_gate="enforce"))
    calls = []
    h._decide_pre_tool = lambda *a: calls.append(a) or {"action": "block", "message": "[jermes] no"}
    h.tool_request_middleware(tool_name="terminal", args={"command": "x"}, session_id="s", tool_call_id="c1")
    assert h.on_pre_tool_call(tool_name="terminal", args={"command": "x"}, session_id="s", tool_call_id="c1") \
        == {"action": "block", "message": "[jermes] no"}
    assert len(calls) == 1                                   # the hook did not ask again
    # consumed: a second hook for the same id decides inline (no stale reuse)
    h.on_pre_tool_call(tool_name="terminal", args={"command": "x"}, session_id="s", tool_call_id="c1")
    assert len(calls) == 2


def test_no_middleware_decides_inline(make_engine):
    h = Harness(make_engine(risk_gate="enforce"))
    h._decide_pre_tool = lambda *a: {"action": "block", "message": "inline"}
    assert h.on_pre_tool_call(tool_name="terminal", args={}, session_id="s", tool_call_id="zz")["message"] == "inline"


def test_decision_found_by_args_when_id_missing_in_middleware(make_engine):
    h = Harness(make_engine(risk_gate="enforce"))
    h._decide_pre_tool = lambda *a: {"action": "block", "message": "m"}
    h.tool_request_middleware(tool_name="terminal", args={"command": "y"}, session_id="s")   # no id
    h._decide_pre_tool = lambda *a: None
    assert h.on_pre_tool_call(tool_name="terminal", args={"command": "y"}, session_id="s", tool_call_id="c9") \
        == {"action": "block", "message": "m"}


def test_slow_decision_fails_open_within_budget(make_engine):
    h = Harness(make_engine(risk_gate="enforce"))
    h.pre_tool_budget_s = 0.2
    h._decide_pre_tool = lambda *a: time.sleep(2) or {"action": "block", "message": "late"}
    t0 = time.monotonic()
    assert h.tool_request_middleware(tool_name="terminal", args={}, session_id="s", tool_call_id="c") is None
    assert time.monotonic() - t0 < 1.0
    assert h.on_pre_tool_call(tool_name="terminal", args={}, session_id="s", tool_call_id="c") is None
