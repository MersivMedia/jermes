"""Jermes configuration.

Read from ``$HERMES_HOME/jermes/config.yaml`` and deep-merged over DEFAULTS.
Every decision point starts in ``shadow`` mode (PRD Phase 0): Jev is consulted
and every would-be action is logged, but nothing about Hermes' behaviour
changes until a point is promoted to ``advise`` or ``enforce``.

Environment overrides:
  JERMES_MODE=off|shadow|advise|enforce   force every point to one mode
                                          (``off`` is the global kill switch)
  JERMES_BACKEND=vercel|typesafe
  JERMES_CONFIG=/path/to/config.yaml
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Mapping

from .store import data_dir

MODES = ("off", "shadow", "advise", "enforce")
POLICY_VERSION = "2026-09-24.1"

DEFAULTS: Dict[str, Any] = {
    "backend": {
        "name": "auto",  # auto | vercel | openrouter | typesafe
        "base_url": None,
        "model": None,
        "deadline_s": 2.5,
        "max_retries": 3,     # retries stop at deadline_s; Vercel 503s are brief and random
        "zero_data_retention": False,
    },
    "redact": True,
    "points": {
        # D1 - skill selection (pre_llm_call)
        "skill_suggest": {
            "mode": "shadow",
            "fits_threshold": 0.50,   # supporting skills must clear their own "would this help" check
            "shortlist": 5,           # candidates carried from the skim into the select call
            "max_listed": 4,          # skills shown to the agent (primary + supporting)
            "excerpt_chars": 700,
            "context_messages": 10,   # earlier user/assistant turns Jev sees (0 = request only)
            "context_chars": 400,     # per message
        },
        # D3 + D4 - argument check and risk gate (pre_tool_call)
        "risk_gate": {
            "mode": "shadow",
            "gated_tools": [
                "terminal", "execute_code", "write_file", "patch", "send_message",
                "cronjob", "browser_click", "browser_type", "skill_manage", "delegate_task",
            ],
            "block_threshold": 0.85,
            "review_threshold": 0.70,     # 0.5 sent 20-40% of real calls to review; see docs/RESULTS.md
            "review_risk_score": 2.5,
            "mismatch_threshold": 0.20,
            "review_on_mismatch": False,  # "not requested" alone flooded review (35% of real calls)
            "context_messages": 6,        # earlier turns, so "ok go ahead" is read against the plan
            "context_chars": 500,
            "max_arg_chars": 4000,
        },
        # D5 - tool-result relevance filter (transform_tool_result)
        "result_filter": {
            "mode": "shadow",
            "tools": ["web_extract", "read_file", "terminal", "session_search", "browser_snapshot"],
            "min_chars": 6000,
            "max_chars": 100000,
            "max_chunks": 120,
            "keep_threshold": 0.35,
            "max_kept_fraction": 0.85,
            "needs_all_threshold": 0.6,   # pass through when the task needs the whole output
        },
        # D6 - loop and completion notes (transform_tool_result)
        "loop_guard": {
            "mode": "shadow",
            "window": 8,
            "completion_every": 3,
            "completion_threshold": 0.85,
        },
        # D7 - per-turn model routing (llm_request middleware)
        "model_router": {
            "mode": "shadow",
            "cheap_model": None,
            "max_difficulty": 0.6,
            "min_confidence": 0.7,
            "max_stakes": 0.3,
            "cache_ttl_s": 300,       # cost check: is the current model's cache still warm?
            "expected_calls": 6,      # cost check: model calls a routed turn is assumed to make
        },
        # X3 - context trimming (context engine; active only with `context: {engine: jermes}`
        # in Hermes' config.yaml). Scores old tool traffic on cold turns; drops become stubs.
        "context_trim": {
            "mode": "shadow",
            "ttl_s": 300,             # provider cache TTL: trim only after a pause this long
            "keep_turns": 2,          # the current and previous user turn are never trimmed
            "min_chars": 1500,        # items smaller than this aren't worth a question
            "keep_threshold": 0.5,    # Jev's "still needed" below this -> stub
            "max_items": 150,
            "excerpt_chars": 700,
            "request_chars": 60000,
        },
        # Ingestion pipeline (explicit tool/CLI, not a hook). Jev only picks
        # among candidates code found; it never writes values.
        "ingest": {
            "mode": "enforce",
            "chunk_chars": 1500,        # I3 chunk size
            "max_chunks": 60,           # chunks screened per document
            "keep_threshold": 0.5,      # chunk kept as evidence when is_relevant >= this
            "injection_threshold": 0.7, # chunk quarantined when contains_instruction >= this
            "pick_min_p": 0.6,          # below this, a picked candidate goes to review
            "none_min_p": 0.6,          # "not stated" accepted only above this
            "flag_threshold": 0.7,      # I6: any verifier flag at or above this escalates
            "class_min_confidence": 0.9,  # I8: below this, report the parent category
            "max_candidates": 250,      # Choice limit is 255; keep room for "none"
            "request_chars": 60000,     # state budget per Jev request (Jev takes ~32k tokens)
            "cheap_model": None,        # I4/I7 generators via Hermes' own LLM access
            "strong_model": None,
        },
    },
}


def _deep_merge(base: Dict[str, Any], over: Mapping[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


# Which key selects which backend when backend.name is "auto". Vercel first
# (the key exists only for gateway use), then TypeSafe direct, then OpenRouter
# last: many Hermes users already have OPENROUTER_API_KEY for their main model,
# and an explicit Jev-specific key should win over it.
AUTO_ORDER = (("vercel", "AI_GATEWAY_API_KEY"), ("typesafe", "TYPESAFE_API_KEY"), ("openrouter", "OPENROUTER_API_KEY"))


def detect_backend() -> str:
    for name, env in AUTO_ORDER:
        if os.environ.get(env, "").strip():
            return name
    return "vercel"  # nothing set: report the Vercel key as missing


def config_path() -> Path:
    return Path(os.environ.get("JERMES_CONFIG") or data_dir() / "config.yaml")


def load_config(overrides: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    path = config_path()
    if path.exists():
        import yaml

        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            loaded = {}
        if isinstance(loaded, Mapping):
            cfg = _deep_merge(cfg, loaded)
    if overrides:
        cfg = _deep_merge(cfg, overrides)

    forced = os.environ.get("JERMES_MODE", "").strip().lower()
    if forced in MODES:
        for point in cfg["points"].values():
            point["mode"] = forced
    backend = os.environ.get("JERMES_BACKEND", "").strip().lower()
    if backend:
        cfg["backend"]["name"] = backend
    if str(cfg["backend"].get("name") or "auto").lower() == "auto":
        cfg["backend"]["name"] = detect_backend()

    for name, point in cfg["points"].items():
        mode = point.get("mode")
        if mode is False:  # YAML 1.1 parses a bare `off` as boolean False
            mode = "off"
        mode = str(mode).strip().lower() if mode is not None else "shadow"
        point["mode"] = mode if mode in MODES else "shadow"
    return cfg
