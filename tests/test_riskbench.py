import json
import sqlite3

from jermes import riskbench
from jermes.risk_cases import CASES


def test_cases_are_well_formed():
    assert {c.label for c in CASES} == {"block", "review", "allow"}
    assert len({c.cid for c in CASES}) == len(CASES)
    assert any(c.kind == "hard_negative" for c in CASES) and any(c.previous_result for c in CASES)


def test_score_cases_math(make_engine, fake):
    # Fake Jev: every call looks maximally destructive and unrequested -> block.
    def r(qid, q, s):
        if qid in ("destructive", "exfiltration"):
            return {"type": "noul", "noul": 0.95}
        if qid == "matches_request":
            return {"type": "choice", "choice": "no", "probabilities": {"yes": 0.0, "partly": 0.0, "no": 1.0,
                                                                        "unclear": 0.0}, "confidence": 1.0}
        return None

    fake.on(r)
    s = riskbench.score_cases(make_engine(risk_gate="enforce"), progress=None)
    assert s["dangerous_blocked"] == 1.0 and s["benign_blocked"] == 1.0 and s["benign_allowed"] == 0.0
    assert s["errors"] == 0 and s["cases"] == len(CASES)


def test_real_calls_pair_each_call_with_its_request(tmp_path):
    db = tmp_path / "state.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT)")
    call = lambda name, args: json.dumps([{"function": {"name": name, "arguments": json.dumps(args)}}])
    c.executemany("INSERT INTO messages (session_id, role, content, tool_calls) VALUES (?,?,?,?)", [
        ("s1", "user", "run the tests", None),
        ("s1", "assistant", "", call("terminal", {"command": "pytest"})),
        ("s1", "assistant", "", call("read_file", {"path": "x"})),          # not gated
        ("s1", "user", "[IMPORTANT: background note]", None),               # synthetic: request unchanged
        ("s1", "assistant", "", call("patch", {"path": "a.py"})),
    ])
    c.commit()
    got = riskbench.real_calls(db, 10)
    assert sorted((g["tool"], g["request"]) for g in got) == [("patch", "run the tests"), ("terminal", "run the tests")]


def test_risk_gate_sees_earlier_conversation(make_engine, fake):
    from jermes.harness import Harness

    h = Harness(make_engine(risk_gate="enforce", skill_suggest="off", model_router="off"))
    hist = [{"role": "user", "content": "plan: delete the old staging bucket, then redeploy"},
            {"role": "assistant", "content": "Ready when you are."}]
    h.on_pre_llm_call(session_id="s", user_message="ok go ahead", conversation_history=hist)
    h.on_pre_tool_call(tool_name="terminal", args={"command": "gsutil rm -r gs://old-staging"}, session_id="s")
    state = fake.requests[-1]["body"]["state"]
    assert "delete the old staging bucket" in state["earlier_conversation"] and state["user_request"] == "ok go ahead"
