"""End-to-end: concurrent sessions must not refuse each other's tool calls.

Hermes keeps one "still running" slot per pre_tool_call callback across all
sessions, and fails the tool call closed when a second call arrives while the
first is still in the callback. With a normal Jev round-trip (~0.3 s) and two
active sessions, that refused real tool calls. Jermes now decides in
tool_request middleware (no such slot), so the hook only looks up the answer.

Hermes builds before upstream commit 4121aa295a (Sept 14, 2026, gate by call
identity) still refuse hooks that start within the same few milliseconds; this
test covers the realistic case, where the old code refused and the new does not.

Runs through Hermes' real PluginManager, in the order Hermes' tool executor
uses: apply_tool_request_middleware, then _dispatch_pre_tool_call_hooks.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
HERMES_DIR = os.environ.get("HERMES_AGENT_DIR", str(Path.home() / "hermes-agent"))

pytestmark = pytest.mark.skipif(not (Path(HERMES_DIR) / "hermes_cli" / "middleware.py").exists(),
                                reason="Hermes Agent checkout with middleware not found")

JEV_DELAY_S = 0.5


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
        "  memory_filter: {mode: off}\n  skill_overlap: {mode: off}\n  context_trim: {mode: off}\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("JERMES_HOME", str(jhome))
    monkeypatch.syspath_prepend(HERMES_DIR)

    def slow_handler(request):
        time.sleep(JEV_DELAY_S)                   # a realistic Jev round-trip
        return fake.handler(request)

    real_init = httpx.Client.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(slow_handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init)
    for mod in [m for m in sys.modules if m in ("hermes_cli.plugins", "hermes_cli.middleware")
                or m.startswith("hermes_plugins")]:
        del sys.modules[mod]
    plugins = importlib.import_module("hermes_cli.plugins")
    middleware = importlib.import_module("hermes_cli.middleware")
    plugins.discover_plugins(force=True)
    yield plugins, middleware
    for mod in [m for m in sys.modules if m.startswith("hermes_plugins")]:
        del sys.modules[mod]


def _tool_call(plugins, middleware, sid, call_id, tool, args, out):
    """The two plugin steps of Hermes' tool executor, in its order."""
    ids = {"task_id": sid, "session_id": sid, "tool_call_id": call_id}
    req = middleware.apply_tool_request_middleware(tool, args, skip_relay=True, **ids)
    final = req.payload if isinstance(req.payload, dict) else args
    block, _ = plugins._dispatch_pre_tool_call_hooks(tool, final, **ids)
    out[sid] = block


def test_jermes_registers_tool_request_middleware(hermes):
    plugins, _ = hermes
    assert plugins.get_plugin_manager()._middleware.get("tool_request")


def test_two_sessions_at_once_are_not_refused(hermes, fake):
    plugins, middleware = hermes
    out: dict = {}
    threads = [
        threading.Thread(target=_tool_call, args=(plugins, middleware, f"s{i}", f"call-{i}", "terminal",
                                                  {"command": f"ls /tmp/dir{i}"}, out))
        for i in range(3)
    ]
    for t in threads:
        t.start()
        time.sleep(0.1)                           # overlap inside one Jev round-trip (0.5 s)
    for t in threads:
        t.join(timeout=30)
    assert len(out) == 3
    refused = {k: v for k, v in out.items() if v and "timed out or is still running" in v}
    assert not refused, f"Hermes refused concurrent tool calls: {refused}"
    assert all(v is None for v in out.values()), out
    assert sum(1 for r in fake.requests if "risk" in r["body"]["questions"]) == 3   # each call was checked


def test_blocks_still_apply_through_the_middleware_path(hermes, fake):
    plugins, middleware = hermes

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
    out: dict = {}
    _tool_call(plugins, middleware, "b1", "call-x", "terminal", {"command": "rm -rf ~/work"}, out)
    assert out["b1"] and "[jermes]" in out["b1"]
    n = len(fake.requests)
    _tool_call(plugins, middleware, "b1", "call-y", "terminal", {"command": "df -h"}, out)
    assert out["b1"] is None
    assert len(fake.requests) == n + 1            # decided once, in middleware; the hook reused it
