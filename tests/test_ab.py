import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from jermes import ab
from jermes.ab_tasks import TASKS


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_task_fixture_fails_until_solved(task, tmp_path):
    task.setup(tmp_path)
    ok, _ = task.check(tmp_path)
    assert not ok  # an untouched fixture must never count as a pass


def test_checks_accept_correct_answers(tmp_path):
    by = {t.name: t for t in TASKS}
    for name, answer in (("big_log", "billing-worker"), ("big_doc", "750 MB"), ("big_json", "1317")):
        d = tmp_path / name
        d.mkdir()
        by[name].setup(d)
        (d / "answer.txt").write_text(answer)
        assert by[name].check(d)[0], name
    d = tmp_path / "fix"
    d.mkdir()
    by["fix_average"].setup(d)
    (d / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs)\n")
    assert by["fix_average"].check(d)[0]


def test_isolated_homes(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # keep test homes out of /tmp
    src = tmp_path / "src"
    (src / "skills").mkdir(parents=True)
    (src / ".env").write_text("ANTHROPIC_API_KEY=x\n")
    (src / "config.yaml").write_text("model: {provider: anthropic, default: m}\nplugins: {enabled: [other]}\n")
    (src / "state.db").write_text("history that must not leak")
    off = ab._make_home(src, "off", {"skill": "advise", "filt": "enforce"})
    on = ab._make_home(src, "on", {"skill": "advise", "filt": "enforce"})
    assert not (off / "state.db").exists() and not (off / "plugins").exists()
    assert "other" not in (off / "config.yaml").read_text()          # the user's plugins never load
    assert (on / "plugins" / "jermes").is_symlink()
    assert "jermes" in (on / "config.yaml").read_text()
    assert "result_filter: {mode: enforce}" in (on / "jermes" / "config.yaml").read_text()
    assert oct((off / ".env").stat().st_mode)[-3:] == "600"


def test_usage_read_from_session_table(tmp_path):
    home = tmp_path
    c = sqlite3.connect(home / "state.db")
    c.execute("CREATE TABLE sessions (model TEXT, api_call_count INT, input_tokens INT, cache_read_tokens INT, "
              "cache_write_tokens INT, output_tokens INT)")
    c.execute("INSERT INTO sessions VALUES ('claude-opus-5-5', 3, 100, 5000, 2000, 300)")
    c.commit()
    c.close()
    u = ab._session_usage(home)
    assert u["api_calls"] == 3 and u["cache_read_tokens"] == 5000


def test_summary_math():
    runs = [ab.Run("t", "off", 0, ok=True, input_tokens=0, cache_read_tokens=100000, usd=0.10),
            ab.Run("t", "on", 0, ok=True, cache_read_tokens=60000, usd=0.06, jev_usd=0.001)]
    s = ab.summarize(runs)
    assert s["overall"]["prompt_token_change_pct"] == -40.0
    assert s["overall"]["on"]["usd"] == pytest.approx(0.061)


def test_preflight_catches_missing_jev_key(tmp_path, monkeypatch):
    for k in ("AI_GATEWAY_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text("x")
    assert any("no Jev key" in p for p in ab.preflight(tmp_path))


def test_clean_env_drops_parent_session(tmp_path, monkeypatch):
    for k, v in {"TERMINAL_CWD": "/elsewhere", "_HERMES_GATEWAY": "1", "HERMES_SESSION_ID": "parent",
                 "HERMES_HOME": "/parent", "JERMES_MODE": "off", "AI_GATEWAY_API_KEY": "k", "PATH": "/usr/bin"}.items():
        monkeypatch.setenv(k, v)
    on = ab.clean_env(tmp_path / "h", tmp_path / "w", "on")
    assert on["TERMINAL_CWD"] == str(tmp_path / "w") and on["HERMES_HOME"] == str(tmp_path / "h")
    assert "_HERMES_GATEWAY" not in on and "HERMES_SESSION_ID" not in on and "JERMES_MODE" not in on
    assert on["AI_GATEWAY_API_KEY"] == "k" and on["PATH"] == "/usr/bin"
    assert "AI_GATEWAY_API_KEY" not in ab.clean_env(tmp_path, tmp_path, "off")
