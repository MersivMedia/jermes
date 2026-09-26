"""Harness behaviour per mode: shadow changes nothing, enforce acts, failures fail open."""

import json
import re
import time

import pytest

from jermes.harness import Harness
from jermes.points import skill_suggest


def _dangerous(fake):
    def r(qid, q, state):
        if not isinstance(state, dict) or state.get("tool") != "terminal":
            return None
        if qid == "destructive":
            return {"type": "noul", "noul": 0.97}
        if qid == "matches_request":
            return {"type": "choice", "choice": "no",
                    "probabilities": {"yes": 0.05, "partly": 0.1, "no": 0.8, "unclear": 0.05}, "confidence": 0.7}
        return None

    fake.on(r)


def _wait_for_rows(eng, n, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if len(eng.store.recent(100)) >= n:
            return
        time.sleep(0.02)
    raise AssertionError("background shadow decision never logged")


def test_shadow_logs_but_never_blocks(make_engine, fake):
    _dangerous(fake)
    h = Harness(make_engine(risk_gate="shadow"))
    h.history.set_request("s", "show me disk usage")
    assert h.on_pre_tool_call(tool_name="terminal", args={"command": "rm -rf ~"}, session_id="s") is None
    _wait_for_rows(h.engine, 1)
    row = h.engine.store.recent(1, point="risk_gate")[0]
    assert row["mode"] == "shadow" and row["action"] == "block" and row["applied"] == 0


def test_shadow_adds_no_latency(make_engine, fake):
    """Shadow decisions run off-thread: a slow Jev must not slow the tool loop."""
    real = fake.handler

    def slow(request):
        time.sleep(0.6)
        return real(request)

    fake.handler = slow  # engine's MockTransport holds a bound method; rebuild it
    import httpx
    eng = make_engine(risk_gate="shadow")
    eng.client._transport = httpx.MockTransport(slow)
    h = Harness(eng)
    t0 = time.monotonic()
    assert h.on_pre_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s") is None
    assert time.monotonic() - t0 < 0.2
    _wait_for_rows(eng, 1, timeout=3.0)


def test_enforce_blocks_dangerous_unrequested(make_engine, fake):
    _dangerous(fake)
    h = Harness(make_engine(risk_gate="enforce"))
    h.history.set_request("s", "show me disk usage")
    d = h.on_pre_tool_call(tool_name="terminal", args={"command": "rm -rf ~"}, session_id="s")
    assert d["action"] == "block" and "[jermes]" in d["message"]
    assert h.engine.store.recent(1)[0]["applied"] == 1


def test_enforce_allows_benign(make_engine, fake):
    h = Harness(make_engine(risk_gate="enforce"))
    h.history.set_request("s", "list files")
    assert h.on_pre_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s") is None


def test_ungated_tool_makes_no_call(make_engine, fake):
    h = Harness(make_engine(risk_gate="enforce"))
    assert h.on_pre_tool_call(tool_name="read_file", args={"path": "x"}, session_id="s") is None
    assert not fake.requests


def test_jev_outage_fails_open(make_engine, fake):
    fake.status = 529
    h = Harness(make_engine(risk_gate="enforce"))
    assert h.on_pre_tool_call(tool_name="terminal", args={"command": "rm -rf ~"}, session_id="s") is None


def test_missing_key_disables_points(make_engine, fake, monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_API_KEY")
    h = Harness(make_engine(risk_gate="enforce"))
    assert h.on_pre_tool_call(tool_name="terminal", args={"command": "x"}, session_id="s") is None
    assert not fake.requests


def _big_result(n=40):
    paras = [("RELEVANT fact %d about the auth token rotation. " % i) * 8 if i % 5 == 0
             else ("cookie banner nav footer boilerplate %d. " % i) * 8 for i in range(n)]
    return json.dumps({"success": True, "content": "\n\n".join(paras)})


def _relevance(fake):
    def r(qid, q, state):
        if qid.startswith("keep_"):
            i = qid.split("_")[1]
            text = state["sections"][f"s{i}"]
            return {"type": "noul", "noul": 0.95 if "RELEVANT" in text else 0.05}
        return None

    fake.on(r)


def test_result_filter_enforce_trims(make_engine, fake):
    _relevance(fake)
    h = Harness(make_engine(result_filter="enforce", loop_guard="off"))
    h.history.set_request("s", "how does token rotation work?")
    original = _big_result()
    out = h.on_transform_tool_result(tool_name="web_extract", args={}, result=original, session_id="s", status="ok")
    assert out is not None and len(out) < len(original) / 2
    content = json.loads(out)["content"]
    assert "RELEVANT fact 0" in content and "boilerplate 1." not in content and "judged not relevant" in content
    # the full output is saved where the note says, so the model can check a detail
    saved = re.search(r"Full output: (\S+?\.txt)", content).group(1)
    assert "boilerplate 1." in open(saved).read()


def test_result_filter_shadow_unchanged(make_engine, fake):
    _relevance(fake)
    h = Harness(make_engine(result_filter="shadow", loop_guard="off"))
    h.history.set_request("s", "how does token rotation work?")
    assert h.on_transform_tool_result(tool_name="web_extract", args={}, result=_big_result(),
                                      session_id="s", status="ok") is None
    _wait_for_rows(h.engine, 1)
    assert h.engine.store.recent(1)[0]["action"] == "filter"


def test_result_filter_small_results_untouched(make_engine, fake):
    h = Harness(make_engine(result_filter="enforce", loop_guard="off"))
    h.history.set_request("s", "q")
    assert h.on_transform_tool_result(tool_name="web_extract", args={}, result='{"content":"short"}',
                                      session_id="s", status="ok") is None
    assert not fake.requests


def test_loop_guard_advise_appends_note(make_engine, fake):
    fake.on(lambda qid, q, s: {"type": "noul", "noul": 0.95} if qid == "repeat_failure" else None)
    h = Harness(make_engine(loop_guard="advise", result_filter="off"))
    h.history.set_request("s", "fix the build")
    assert h.on_transform_tool_result(tool_name="terminal", args={"command": "make"},
                                      result='{"error":"fail"}', session_id="s", status="error") is None
    out = h.on_transform_tool_result(tool_name="terminal", args={"command": "make"},
                                     result='{"error":"fail"}', session_id="s", status="error")
    assert "repeats an earlier call" in json.loads(out)["_harness_note"]


def test_model_router_enforce_swaps_model_for_turn(make_engine, fake):
    eng = make_engine(model_router="enforce", skill_suggest="off")
    eng.config["points"]["model_router"]["cheap_model"] = "claude-haiku-4-5"
    h = Harness(eng)
    small = [{"role": "user", "content": "hi"}]
    h.llm_request_middleware(request={"model": "claude-opus-5", "messages": small}, session_id="s")  # a prior turn
    h._last_llm["s"] -= 1000                                            # ...long enough ago that the cache is cold
    h.on_pre_llm_call(session_id="s", user_message="what time is it in Tokyo?")
    out = h.llm_request_middleware(request={"model": "claude-opus-5", "messages": small}, session_id="s")
    assert out["request"]["model"] == "claude-haiku-4-5"
    assert h.llm_request_middleware(request={"model": "claude-opus-5"}, session_id="other") is None


def test_model_router_without_cheap_model_is_inert(make_engine, fake):
    h = Harness(make_engine(model_router="enforce", skill_suggest="off"))
    h.on_pre_llm_call(session_id="s", user_message="hi")
    assert h.llm_request_middleware(request={"model": "big"}, session_id="s") is None


def _deck_ranker(fake, seen_states=None):
    def r(qid, q, state):
        if seen_states is not None:
            seen_states.append(state)
        req = state.get("request", "") if isinstance(state, dict) else ""
        if qid == "which":
            opts = list(q["criteria"])
            pick = "pptx-author" if ("pptx" in req or "deck" in state.get("recent_context", "")) else skill_suggest.NONE_OPTION
            probs = {o: (0.8 if o == pick else 0.2 / (len(opts) - 1)) for o in opts}
            return {"type": "choice", "choice": pick, "probabilities": probs, "confidence": 0.7}
        if qid.startswith("fits::"):
            return {"type": "noul", "noul": 0.8 if "pptx" in qid else 0.05}
        return None

    fake.on(r)


DECK_ROSTER = [
    skill_suggest.Skill("powerpoint", "Create, read, edit .pptx decks", "edit existing decks"),
    skill_suggest.Skill("pptx-author", "Build decks with python-pptx", "author new decks"),
    skill_suggest.Skill("apple-notes", "Manage Apple Notes", "notes"),
]


def test_skill_suggest_advise_two_stage(make_engine, fake):
    _deck_ranker(fake)
    h = Harness(make_engine(skill_suggest="advise", model_router="off"))
    h._roster = DECK_ROSTER
    out = h.on_pre_llm_call(session_id="s", user_message="make me a pitch deck as a .pptx")
    assert out and "pptx-author" in out["context"]
    assert len(fake.requests) == 2  # skim + select


def test_skill_suggest_none_option_stops_after_skim(make_engine, fake):
    _deck_ranker(fake)
    h = Harness(make_engine(skill_suggest="advise", model_router="off"))
    h._roster = DECK_ROSTER
    out = h.on_pre_llm_call(session_id="s", user_message="explain what a monad is")
    assert "No skill is needed" in out["context"] and len(fake.requests) == 1


def test_skill_suggest_uses_conversation_history(make_engine, fake):
    states = []
    _deck_ranker(fake, states)
    h = Harness(make_engine(skill_suggest="advise", model_router="off"))
    h._roster = DECK_ROSTER
    history = [
        {"role": "user", "content": "I need a board deck for Friday"},
        {"role": "assistant", "content": "Sure, what should the deck cover?"},
        {"role": "user", "content": "yes go ahead and build it"},
    ]
    out = h.on_pre_llm_call(session_id="s", user_message="yes go ahead and build it", conversation_history=history)
    assert "pptx-author" in out["context"]  # only resolvable through the earlier turns
    ctx = states[0]["recent_context"]
    assert "board deck" in ctx and "go ahead and build it" not in ctx  # request is not duplicated as context


@pytest.mark.parametrize("hook", ["on_pre_tool_call", "on_transform_tool_result", "on_pre_llm_call"])
def test_hooks_never_raise_on_garbage(make_engine, hook):
    h = Harness(make_engine(risk_gate="enforce", result_filter="enforce", loop_guard="enforce",
                            skill_suggest="enforce", model_router="enforce"))
    getattr(h, hook)(tool_name=None, args=None, result=None, session_id=None, user_message=None)


def test_result_filter_skips_explicit_line_ranges(make_engine, fake):
    _relevance(fake)
    h = Harness(make_engine(result_filter="enforce", loop_guard="off"))
    h.history.set_request("s", "how does token rotation work?")
    out = h.on_transform_tool_result(tool_name="read_file", args={"path": "doc.md", "offset": 200, "limit": 300},
                                     result=_big_result(), session_id="s", status="ok")
    assert out is None and not fake.requests


def test_router_guard_blocks_switch_when_context_too_big(make_engine, fake):
    fake.on(lambda qid, q, s: {"type": "score", "score": 0.0, "probabilities": {"0": 1.0, "1": 0.0, "2": 0.0},
                               "confidence": 0.95} if qid == "difficulty" else None)
    eng = make_engine(model_router="enforce", skill_suggest="off", risk_gate="off")
    eng.config["points"]["model_router"]["cheap_model"] = "claude-haiku-4-5"
    h = Harness(eng)
    big = [{"role": "user", "content": "x" * 1_000_000}]                      # ~250k tokens
    h.llm_request_middleware(request={"model": "claude-opus-5-5", "messages": big}, session_id="s")
    h.on_pre_llm_call(session_id="s", user_message="what time is it?")
    assert h.llm_request_middleware(request={"model": "claude-opus-5-5", "messages": big}, session_id="s") is None
    # First turn of a session: the current model is unknown, so no switch.
    h2 = Harness(eng)
    h2.on_pre_llm_call(session_id="t", user_message="what time is it?")
    assert h2.llm_request_middleware(request={"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]},
                                     session_id="t") is None
