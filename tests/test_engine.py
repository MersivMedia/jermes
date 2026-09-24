import pytest
import json

from jermes.engine import Verdict
from jermes.questions import Noul
from jermes.store import cache_key, canonical_json


def test_canonical_state_ignores_order_and_volatile_keys():
    a = {"b": 1, "a": {"y": 2, "x": 1}, "timestamp": 1}
    b = {"a": {"x": 1, "y": 2}, "b": 1, "timestamp": 999, "tool_call_id": "z"}
    assert canonical_json(a) == canonical_json(b)
    assert cache_key("m", a, {"q": 1}) == cache_key("m", b, {"q": 1})
    assert cache_key("m", a, {"q": 1}) != cache_key("m2", a, {"q": 1})


def test_cache_replays_identical_decision(make_engine, fake):
    eng = make_engine(risk_gate="enforce")
    pol = lambda a: Verdict("yes" if a["u"].noul > 0.5 else "no")
    d1 = eng.decide("risk_gate", {"x": 1}, {"u": Noul("q")}, pol)
    fake.on(lambda qid, q, s: {"type": "noul", "noul": 0.99})  # model "drifts"
    d2 = eng.decide("risk_gate", {"x": 1}, {"u": Noul("q")}, pol)
    assert len(fake.requests) == 1  # second call never hit the network
    assert d1.action == d2.action == "no" and d2.cached
    d3 = eng.decide("risk_gate", {"x": 2}, {"u": Noul("q")}, pol)
    assert d3.action == "yes" and not d3.cached


def test_failure_is_logged_and_never_raises(make_engine, fake):
    eng = make_engine(risk_gate="enforce")
    fake.status = 503
    d = eng.decide("risk_gate", "s", {"u": Noul("q")}, lambda a: Verdict("x"))
    assert d.action is None and d.error and not d.enforcing
    row = eng.store.recent(1)[0]
    assert row["error"] and row["point"] == "risk_gate"


def test_policy_exception_is_contained(make_engine):
    eng = make_engine(risk_gate="enforce")

    def bad(a):
        raise KeyError("oops")

    d = eng.decide("risk_gate", "s", {"u": Noul("q")}, bad)
    assert d.error.startswith("internal") and d.action is None


def test_off_mode_makes_no_call(make_engine, fake):
    eng = make_engine(risk_gate="off")
    d = eng.decide("risk_gate", "s", {"u": Noul("q")}, lambda a: Verdict("x"))
    assert d.error == "off" and not fake.requests


def test_redacts_unknown_prefix_keys_but_not_identifiers():
    from jermes.engine import _redact

    fake_key = "vck_" + "A1b2C3d4E5f6G7h8I9j0KlMnOpQrStUv"
    out = _redact({"request": f"here is my key {fake_key} thanks"})["request"]
    assert fake_key not in out and "[REDACTED]" in out
    keep = "load generative-media-pipeline-design and executive_stakeholder_research_v2 for session 20260924_211008_e9fbc1"
    assert _redact({"request": keep})["request"] == keep


def test_config_yaml_off_is_off(tmp_path, monkeypatch):
    from jermes.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text("points:\n  risk_gate: {mode: off}\n  loop_guard: {mode: enforce}\n  result_filter: {mode: bogus}\n")
    monkeypatch.setenv("JERMES_CONFIG", str(p))
    cfg = load_config()
    assert cfg["points"]["risk_gate"]["mode"] == "off"
    assert cfg["points"]["loop_guard"]["mode"] == "enforce"
    assert cfg["points"]["result_filter"]["mode"] == "shadow"
    monkeypatch.setenv("JERMES_MODE", "off")
    assert all(pt["mode"] == "off" for pt in load_config()["points"].values())


def test_log_row_contents(make_engine):
    eng = make_engine(risk_gate="shadow")
    d = eng.decide("risk_gate", {"a": 1}, {"u": Noul("q")}, lambda a: Verdict("allow", {"k": 1}),
                   session_id="s1", spec_version="v9")
    eng.mark_applied(d)
    row = eng.store.recent(1)[0]
    assert row["session_id"] == "s1" and row["spec_version"] == "v9" and row["mode"] == "shadow"
    assert row["action"] == "allow" and row["applied"] == 1 and row["input_tokens"] == 123
    assert json.loads(row["answers_json"])["u"]["noul"] == 0.1
    assert json.loads(row["detail_json"])["k"] == 1


@pytest.mark.parametrize("env,expected", [
    ({"AI_GATEWAY_API_KEY": "v"}, "vercel"),
    ({"OPENROUTER_API_KEY": "o"}, "openrouter"),
    ({"TYPESAFE_API_KEY": "t", "OPENROUTER_API_KEY": "o"}, "typesafe"),  # Jev-specific key beats a general one
    ({"AI_GATEWAY_API_KEY": "v", "OPENROUTER_API_KEY": "o"}, "vercel"),
    ({}, "vercel"),
])
def test_auto_backend_detection(tmp_path, monkeypatch, env, expected):
    from jermes.config import load_config

    monkeypatch.setenv("JERMES_CONFIG", str(tmp_path / "none.yaml"))
    for k in ("AI_GATEWAY_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "JERMES_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert load_config()["backend"]["name"] == expected


def test_explicit_backend_beats_auto(tmp_path, monkeypatch):
    from jermes.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text("backend: {name: openrouter}\n")
    monkeypatch.setenv("JERMES_CONFIG", str(p))
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "v")
    assert load_config()["backend"]["name"] == "openrouter"
