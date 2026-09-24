"""End-to-end: load Jermes through Hermes' real PluginManager.

Skipped unless a Hermes Agent checkout is importable (set HERMES_AGENT_DIR or
install hermes-agent). Uses a temp HERMES_HOME, installs the repo as a user
plugin by symlink, enables it in config.yaml, and fires hooks through Hermes'
own dispatch functions -- no mocks of the plugin system. Jev itself is faked
at the HTTP layer so no network or spend is involved.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
HERMES_DIR = os.environ.get("HERMES_AGENT_DIR", str(Path.home() / "hermes-agent"))


def _hermes_available() -> bool:
    return (Path(HERMES_DIR) / "hermes_cli" / "plugins.py").exists()


pytestmark = pytest.mark.skipif(not _hermes_available(), reason="Hermes Agent checkout not found")


@pytest.fixture
def hermes(tmp_path, monkeypatch, fake):
    home = tmp_path / "hermes"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "jermes").symlink_to(REPO, target_is_directory=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [jermes]\n")
    jhome = tmp_path / "jermes"
    jhome.mkdir()
    (jhome / "config.yaml").write_text(
        "points:\n  risk_gate: {mode: enforce}\n  result_filter: {mode: off}\n"
        "  loop_guard: {mode: off}\n  skill_suggest: {mode: off}\n  model_router: {mode: off}\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("JERMES_HOME", str(jhome))
    monkeypatch.syspath_prepend(HERMES_DIR)

    # Route every Jev call to the offline fake. Hermes imports the plugin under
    # its own module name (hermes_plugins.jermes...), so patch httpx itself
    # rather than a jermes module object.
    real_init = httpx.Client.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init)

    for mod in [m for m in sys.modules if m == "hermes_cli.plugins" or m.startswith("hermes_plugins")]:
        del sys.modules[mod]
    plugins = importlib.import_module("hermes_cli.plugins")
    plugins.discover_plugins(force=True)
    yield plugins
    for mod in [m for m in sys.modules if m.startswith("hermes_plugins")]:
        del sys.modules[mod]


def test_plugin_loads_and_registers(hermes):
    mgr = hermes.get_plugin_manager()
    names = {getattr(p, "name", None) or getattr(getattr(p, "manifest", None), "name", None)
             for p in (mgr._plugins.values() if isinstance(mgr._plugins, dict) else mgr._plugins)}
    assert "jermes" in names, f"loaded plugins: {names}"
    for hook in ("pre_llm_call", "pre_tool_call", "transform_tool_result"):
        assert mgr._hooks.get(hook), f"no callbacks for {hook}"
    assert mgr._middleware.get("llm_request")


def test_real_dispatch_blocks_and_allows(hermes, fake):
    def danger(qid, q, state):
        if isinstance(state, dict) and "rm -rf" in state.get("arguments", ""):
            if qid == "destructive":
                return {"type": "noul", "noul": 0.97}
            if qid == "matches_request":
                return {"type": "choice", "choice": "no",
                        "probabilities": {"yes": 0.05, "partly": 0.1, "no": 0.8, "unclear": 0.05},
                        "confidence": 0.7}
        return None

    fake.on(danger)
    hermes.invoke_hook("pre_llm_call", session_id="e2e", user_message="how much disk do I have?",
                       conversation_history=[], is_first_turn=True, model="m", platform="cli")
    block, _ = hermes._dispatch_pre_tool_call_hooks("terminal", {"command": "rm -rf ~/work"},
                                                    task_id="e2e", session_id="e2e")
    assert block and "[jermes]" in block
    block, _ = hermes._dispatch_pre_tool_call_hooks("terminal", {"command": "df -h"},
                                                    task_id="e2e", session_id="e2e")
    assert block is None
    sent = [r["body"] for r in fake.requests if "risk" in r["body"]["questions"]]
    assert sent and json.loads(json.dumps(sent[0]))["model"] == "typesafe-ai/jev"
