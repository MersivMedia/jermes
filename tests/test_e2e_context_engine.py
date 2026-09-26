"""End-to-end: Hermes loads the Jermes context engine and calls it per request.

Uses Hermes' real plugin system and its real request hook
(``_apply_context_engine_selection``), with Jev faked at the HTTP layer.
Skipped unless a Hermes Agent checkout with ``select_context`` is present.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
HERMES_DIR = os.environ.get("HERMES_AGENT_DIR", str(Path.home() / "hermes-agent"))


def _available() -> bool:
    p = Path(HERMES_DIR) / "agent" / "context_engine.py"
    return p.exists() and "def select_context" in p.read_text()


pytestmark = pytest.mark.skipif(not _available(), reason="Hermes with select_context not found")


def test_hermes_uses_jermes_engine_and_trims_a_request(tmp_path, monkeypatch, fake):
    home = tmp_path / "hermes"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "jermes").symlink_to(REPO, target_is_directory=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [jermes]\ncontext:\n  engine: jermes\n")
    jhome = tmp_path / "jermes"
    jhome.mkdir()
    (jhome / "config.yaml").write_text(
        "points:\n  context_trim: {mode: enforce}\n  risk_gate: {mode: off}\n  result_filter: {mode: off}\n"
        "  loop_guard: {mode: off}\n  skill_suggest: {mode: off}\n  model_router: {mode: off}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("JERMES_HOME", str(jhome))
    monkeypatch.syspath_prepend(HERMES_DIR)
    real_init = httpx.Client.__init__

    def init(self, *a, **k):
        k["transport"] = httpx.MockTransport(fake.handler)
        real_init(self, *a, **k)

    monkeypatch.setattr(httpx.Client, "__init__", init)
    fake.on(lambda qid, q, s: {"type": "noul", "noul": 0.05} if qid.startswith("need_") else None)
    for mod in [m for m in sys.modules if m == "hermes_cli.plugins" or m.startswith("hermes_plugins")]:
        del sys.modules[mod]

    from hermes_cli import plugins

    plugins.discover_plugins()
    engine = plugins.get_plugin_context_engine()
    assert engine is not None and engine.name == "jermes"

    # Hermes deep-copies the registered engine per agent; the copy must work.
    import copy

    engine = copy.deepcopy(engine)
    engine.update_model(model="claude-opus-5-5", context_length=1_000_000, provider="anthropic")
    assert engine.context_length == 1_000_000

    big = "old build log line\n" * 200
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "build it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "make"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": big},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "now test it"},
        {"role": "assistant", "content": "tested"},
        {"role": "user", "content": "ship it"},
    ]

    class Agent:
        context_compressor = engine
        session_id = "e2e"

    import logging

    from agent.conversation_loop import _apply_context_engine_selection

    out = _apply_context_engine_selection(Agent(), msgs, msgs, msgs[-1], logger=logging.getLogger("t"))
    tool = next(m for m in out if m.get("tool_call_id") == "t1")
    assert tool["content"].startswith("[jermes: output of terminal")
    assert msgs[3]["content"] == big                        # Hermes' own list untouched
    assert any("need_0" in r["body"]["questions"] for r in fake.requests)
