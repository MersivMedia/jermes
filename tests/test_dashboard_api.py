"""Dashboard API: status, decisions, counts, cost, mode toggles (with backup), audit guard."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
fastapi = pytest.importorskip("fastapi")


def _api(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "jermes").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("JERMES_HOME", raising=False)        # the conftest points it elsewhere
    monkeypatch.delenv("JERMES_CONFIG", raising=False)
    monkeypatch.delenv("JERMES_MODE", raising=False)
    spec = importlib.util.spec_from_file_location("jermes_dash_api", REPO / "dashboard" / "plugin_api.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, home


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


CONFIG = """# my notes stay
points:
  context_trim: {mode: shadow}
  risk_gate: {mode: shadow, review_threshold: 0.7}   # tuned
  skill_suggest:
    mode: off
  loop_guard: {mode: enforce}
"""


def _log(home, rows):
    """Store.log stamps the current time, so backdate each row afterwards to its own ts."""
    import sqlite3
    from jermes.store import Store
    s = Store(home / "jermes" / "decisions.sqlite")
    ids = [(s.log(**r), r["ts"]) for r in rows]
    s.close()
    c = sqlite3.connect(home / "jermes" / "decisions.sqlite")
    c.executemany("UPDATE decisions SET ts=? WHERE id=?", [(ts, i) for i, ts in ids])
    c.commit()
    c.close()


def test_mode_toggle_keeps_comments_and_other_settings(monkeypatch, tmp_path):
    api, home = _api(monkeypatch, tmp_path)
    cfg = home / "jermes" / "config.yaml"
    cfg.write_text(CONFIG)
    r = run(api.set_mode({"point": "risk_gate", "mode": "off"}))
    assert r["previous"] == "shadow" and r["mode"] == "off" and Path(r["backup"]).read_text() == CONFIG
    text = cfg.read_text()
    assert "# my notes stay" in text and "# tuned" in text
    pts = yaml.safe_load(text)["points"]
    from jermes.config import load_config
    assert load_config()["points"]["risk_gate"]["mode"] == "off"
    assert pts["risk_gate"]["review_threshold"] == 0.7
    assert pts["context_trim"]["mode"] == "shadow"
    run(api.set_mode({"point": "skill_suggest", "mode": "advise"}))             # block style
    assert yaml.safe_load(cfg.read_text())["points"]["skill_suggest"]["mode"] == "advise"
    run(api.set_mode({"point": "memory_filter", "mode": "off"}))                # not in the file yet
    from jermes.config import load_config
    assert load_config()["points"]["memory_filter"]["mode"] == "off"          # read the way the agent reads it


def test_mode_toggle_refuses_unknown_and_meaningless_modes(monkeypatch, tmp_path):
    api, home = _api(monkeypatch, tmp_path)
    (home / "jermes" / "config.yaml").write_text(CONFIG)
    for body in ({"point": "nope", "mode": "off"},
                 {"point": "risk_gate", "mode": "advise"},              # advise would act like shadow
                 {"point": "risk_gate", "mode": "enforce"},             # enforce without confirm
                 {"point": "risk_gate", "mode": "enforce", "confirm": "yes"}):
        with pytest.raises(fastapi.HTTPException) as e:
            run(api.set_mode(body))
        assert e.value.status_code == 400, body
    assert (home / "jermes" / "config.yaml").read_text() == CONFIG


def test_enforce_with_confirm_and_back(monkeypatch, tmp_path):
    api, home = _api(monkeypatch, tmp_path)
    (home / "jermes" / "config.yaml").write_text(CONFIG)
    from jermes.config import load_config
    r = run(api.set_mode({"point": "risk_gate", "mode": "enforce", "confirm": True}))
    assert r["mode"] == "enforce" and load_config()["points"]["risk_gate"]["mode"] == "enforce"
    run(api.set_mode({"point": "loop_guard", "mode": "shadow"}))             # leaving enforce is allowed
    assert load_config()["points"]["loop_guard"]["mode"] == "shadow"
    s = run(api.status())
    rg = next(p for p in s["points"] if p["point"] == "risk_gate")
    assert rg["modes"] == ["off", "shadow", "enforce"] and "refused" in rg["enforce_effect"]


def test_mode_toggle_creates_config_when_missing(monkeypatch, tmp_path):
    api, home = _api(monkeypatch, tmp_path)
    run(api.set_mode({"point": "risk_gate", "mode": "shadow"}))
    assert yaml.safe_load((home / "jermes" / "config.yaml").read_text())["points"]["risk_gate"]["mode"] == "shadow"


def test_status_never_returns_key_values(monkeypatch, tmp_path):
    api, home = _api(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck_supersecretvalue1234567890")
    s = run(api.status())
    assert s["key_present"] is True and "supersecret" not in json.dumps(s)
    assert {p["point"] for p in s["points"]} >= {"risk_gate", "context_trim", "memory_filter"}
    modes = {p["point"]: p["modes"] for p in s["points"]}
    assert modes["risk_gate"] == ["off", "shadow", "enforce"]
    assert modes["memory_filter"] == ["off", "shadow", "advise", "enforce"]


def test_decisions_counts_and_cost(monkeypatch, tmp_path):
    api, home = _api(monkeypatch, tmp_path)
    now = time.time()
    base = dict(session_id="s1", mode="shadow", spec_version="x", policy_version="p", model="jev",
                state_hash="h", answers_json="{}", applied=0, cached=0, latency_ms=200)
    _log(home, [
        {**base, "ts": now - 60, "point": "risk_gate", "action": "review", "input_tokens": 1000,
         "detail_json": json.dumps({"tool": "terminal", "preview": "git push", "reason": "high-risk action"})},
        {**base, "ts": now - 30, "point": "risk_gate", "action": "allow", "input_tokens": 1000, "detail_json": "{}"},
        {**base, "ts": now - 20, "point": "context_trim.score", "action": "scored", "input_tokens": 5000,
         "detail_json": "{}"},
        {**base, "ts": now - 10, "point": "risk_gate", "action": "allow", "input_tokens": 1000, "cached": 1,
         "detail_json": "{}"},
    ])
    rows = run(api.decisions(point="", action="", limit=10, flagged=True))["rows"]
    assert [r["action"] for r in rows] == ["review"] and rows[0]["preview"] == "git push"
    assert all("." not in r["point"] for r in run(api.decisions(point="", action="", limit=10, flagged=False))["rows"])
    c = run(api.counts(hours=24))["points"]["risk_gate"]
    assert c["checked"] == 3 and c["review"] == 1 and c["allow"] == 2
    cost = run(api.cost(days=7))
    assert len(cost["days"]) == 7 and cost["all_time_requests"] == 3             # cached call is free
    assert cost["all_time_usd"] == round(7000 * 0.042 / 1e6, 5)


def test_empty_install_does_not_error(monkeypatch, tmp_path):
    api, home = _api(monkeypatch, tmp_path)
    assert run(api.decisions(point="", action="", limit=5, flagged=False)) == {"rows": []}
    assert run(api.counts(hours=24))["points"] == {}
    assert run(api.cost(days=3))["today_usd"] == 0
    assert run(api.trimreport(days=14))["items"] == 0
    assert run(api.skills_audit_status())["state"] == "idle"


def test_second_audit_is_refused_while_running(monkeypatch, tmp_path):
    api, home = _api(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "_run_audit", lambda: time.sleep(0.3))
    assert run(api.skills_audit_start())["state"] == "running"
    with pytest.raises(fastapi.HTTPException) as e:
        run(api.skills_audit_start())
    assert e.value.status_code == 409


def test_engine_picks_up_mode_changes_without_restart(monkeypatch, tmp_path):
    api, home = _api(monkeypatch, tmp_path)
    cfg = home / "jermes" / "config.yaml"
    cfg.write_text("points:\n  risk_gate: {mode: off}\n")
    from jermes.engine import Engine
    eng = Engine()
    assert eng.mode("risk_gate") == "off"
    run(api.set_mode({"point": "risk_gate", "mode": "shadow"}))
    eng._cfg_checked = 0.0                     # skip the 2-second recheck interval
    import os
    os.utime(cfg, (time.time() + 5, time.time() + 5))
    assert eng.mode("risk_gate") == "shadow"


def test_manifest_points_at_real_files():
    m = json.loads((REPO / "dashboard" / "manifest.json").read_text())
    assert m["name"] == "jermes" and m["tab"]["path"] == "/jermes"
    for key in ("entry", "css", "api"):
        assert (REPO / "dashboard" / m[key]).is_file(), key
    js = (REPO / "dashboard" / m["entry"]).read_text()
    assert 'register("jermes"' in js and "fetch(" not in js.replace("fetchJSON(", "")   # only the SDK client


def test_windows_exclude_old_decisions(monkeypatch, tmp_path):
    api, home = _api(monkeypatch, tmp_path)
    now = time.time()
    base = dict(session_id="s", mode="shadow", spec_version="x", policy_version="p", model="jev", state_hash="h",
                answers_json="{}", applied=0, cached=0, latency_ms=1, detail_json="{}", point="risk_gate",
                action="allow")
    _log(home, [{**base, "ts": now - 3 * 86400, "input_tokens": 100000},
                {**base, "ts": now - 60, "input_tokens": 1000}])
    assert run(api.counts(hours=24))["points"]["risk_gate"]["checked"] == 1
    cost = run(api.cost(days=7))
    assert sum(d["requests"] for d in cost["days"] if d["requests"]) == 2
    assert [d["day"] for d in cost["days"] if d["tokens"] == 100000]               # lands on its own day
    assert cost["today_usd"] < cost["week_usd"]
