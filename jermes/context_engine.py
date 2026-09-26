"""Jev context engine: trim old tool traffic out of each request (PRD X3).

Most of a long session's prompt is tool traffic from finished steps: command
output, file contents, and the big arguments the agent itself wrote (whole
files, patches, scripts). Every model call re-sends all of it.

This engine keeps Hermes' own compressor for compaction and adds one
per-request step through ``ContextEngine.select_context()``:

* **Only on a cold turn.** When the provider's prompt cache has expired (no
  call for longer than the cache TTL), the next request has to be written to
  the cache in full anyway, so changing old content costs nothing extra. At
  that moment Jev scores every old item: "will this still be needed?"
* **Items Jev drops become short stubs** that say what they were and where
  the full text is saved on disk. Kept items stay verbatim.
* **Stubs are re-applied byte-for-byte on every later request**, so the
  trimmed prefix is what gets cached and read back cheaply for the rest of
  the session. Nothing changes mid-loop.
* **Request-only.** The transcript Hermes stores is never modified.
* **Fails open.** No Jev key, a Jev error, or anything unexpected leaves the
  request unchanged.

Enable with ``context: {engine: jermes}`` in Hermes' config.yaml, then set
``points.context_trim.mode`` in Jermes' config (``shadow`` logs what it would
trim; ``enforce`` trims).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .engine import Verdict
from .questions import Noul

logger = logging.getLogger(__name__)

SPEC_VERSION = "context_trim.1"
POINT = "context_trim"
PROTECTED_TOOLS = {"todo", "memory", "clarify"}

# The Jermes Engine (client, cache, log) is shared at module level rather than
# stored on the context engine: Hermes deep-copies the registered engine for
# every agent, and clients/locks/sqlite handles can't be copied.
_shared = None
_shared_lock = threading.Lock()


# Trimmer state per Hermes session id, at module level: the gateway replaces
# agent instances (idle eviction, memory pressure, restarts of the agent
# cache) and each gets a fresh deep copy of the engine, but the clock and the
# stubs belong to the conversation. Bounded; oldest sessions are dropped.
_trimmers: "Dict[str, Trimmer]" = {}
_trimmers_lock = threading.Lock()
_MAX_SESSIONS = 256


def trimmer_for(session_id: str) -> "Trimmer":
    with _trimmers_lock:
        t = _trimmers.pop(session_id, None) or Trimmer(session_id)
        _trimmers[session_id] = t              # re-insert: most recent last
        while len(_trimmers) > _MAX_SESSIONS:
            _trimmers.pop(next(iter(_trimmers)))
        return t


def forget_session(session_id: str) -> None:
    with _trimmers_lock:
        _trimmers.pop(session_id, None)


def set_shared_engine(engine: Any) -> None:
    global _shared
    _shared = engine


def shared_engine():
    global _shared
    with _shared_lock:
        if _shared is None:
            from .engine import Engine

            _shared = Engine()
        return _shared


# ---------------------------------------------------------------- helpers

def _is_real_user(m: Dict[str, Any]) -> bool:
    if m.get("role") != "user":
        return False
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return isinstance(c, str) and bool(c.strip()) and not c.lstrip().startswith("[")


def _text(m: Dict[str, Any]) -> str:
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c if isinstance(c, str) else ""


def _save(text: str, directory: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:16]}.txt"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(text)
    return str(path)


def _short_args(args: str, limit: int = 120) -> str:
    try:
        d = json.loads(args)
    except (TypeError, ValueError):
        return " ".join(args.split())[:limit]
    if isinstance(d, dict):
        for k in ("command", "path", "url", "query", "name", "goal", "pattern"):
            if isinstance(d.get(k), str):
                return f"{k}={' '.join(d[k].split())[:limit]}"
    return " ".join(json.dumps(d)[:limit].split())


def stub_result(tool: str, args: str, text: str, path: str) -> str:
    return (f"[jermes: output of {tool} ({_short_args(args)}) from an earlier step, {len(text):,} characters, "
            f"trimmed as no longer needed. Full text: {path}]")


def stub_args(args: str, path: str, keep_chars: int = 200) -> Optional[str]:
    """Shorten long string values inside tool-call arguments; stays valid JSON.

    Returns None when the arguments aren't a JSON object (left untouched:
    providers parse tool arguments, so a broken string could fail the request).
    """
    try:
        d = json.loads(args)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    out = {}
    changed = False
    for k, v in d.items():
        if isinstance(v, str) and len(v) > keep_chars + 100:
            out[k] = f"{v[:keep_chars]} ...[jermes: {len(v) - keep_chars:,} more characters trimmed; full arguments: {path}]"
            changed = True
        else:
            out[k] = v
    return json.dumps(out, ensure_ascii=False) if changed else None


# ---------------------------------------------------------------- trimmer

class Trimmer:
    """Hermes-independent trimming logic. One per agent (session)."""

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        self.stubs: Dict[str, str] = {}      # "<tool_call_id>:r" / ":a" -> replacement
        self.last_call: Optional[float] = None
        self.stats = {"cold_turns": 0, "decisions": 0, "trimmed_items": 0, "trimmed_chars": 0, "errors": 0}

    # -- config --------------------------------------------------------------

    @staticmethod
    def cfg() -> Dict[str, Any]:
        return shared_engine().point_config(POINT)

    # -- the per-request step -------------------------------------------------

    def apply(self, messages: List[Dict[str, Any]], now: Optional[float] = None) -> Optional[List[Dict[str, Any]]]:
        eng = shared_engine()
        cfg = eng.point_config(POINT)
        mode = str(cfg.get("mode", "off"))
        if mode == "off":
            return None
        now = time.time() if now is None else now
        ttl = float(cfg.get("ttl_s", 300))
        cold = self.last_call is None or (now - self.last_call) > ttl
        self.last_call = now
        if cold and eng.client.available():
            self.stats["cold_turns"] += 1
            if mode == "shadow":
                threading.Thread(target=self._decide, args=(list(messages), cfg, True), daemon=True).start()
            else:
                self._decide(messages, cfg, False)
        if mode != "enforce" or not self.stubs:
            return None
        return self._render(messages)

    # -- deciding what to trim (cold turns only) -----------------------------

    def candidates(self, messages: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        keep_turns = int(cfg.get("keep_turns", 2))
        min_chars = int(cfg.get("min_chars", 1500))
        turn = 0
        turns: List[int] = []
        for m in messages:
            if _is_real_user(m):
                turn += 1
            turns.append(turn)
        current = turn
        calls: Dict[str, Tuple[str, str]] = {}
        out = []
        for m, t in zip(messages, turns):
            old = t <= current - keep_turns
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    f = tc.get("function") or {}
                    cid = tc.get("id") or tc.get("call_id") or ""
                    name, args = f.get("name", ""), f.get("arguments") or ""
                    calls[cid] = (name, args)
                    if old and cid and len(args) >= min_chars and name not in PROTECTED_TOOLS:
                        out.append({"key": f"{cid}:a", "tool": name, "kind": "input the agent wrote",
                                    "text": args, "args": args})
            elif m.get("role") == "tool" and old:
                cid = m.get("tool_call_id") or ""
                text = m.get("content")
                name, args = calls.get(cid, (m.get("name") or m.get("tool_name") or "", ""))
                if cid and isinstance(text, str) and len(text) >= min_chars and name not in PROTECTED_TOOLS:
                    out.append({"key": f"{cid}:r", "tool": name, "kind": "tool output", "text": text, "args": args})
        out.sort(key=lambda c: -len(c["text"]))
        return out[: int(cfg.get("max_items", 150))]

    def _decide(self, messages: List[Dict[str, Any]], cfg: Dict[str, Any], shadow: bool) -> None:
        """Re-decide every old item from scratch (the cache is cold, so any change is free).

        An item trimmed at an earlier cold turn comes back verbatim if the new
        request needs it. On any error the stubs are cleared: the request goes
        out unchanged.
        """
        try:
            cands = self.candidates(messages, cfg)
            if not cands:
                if not shadow:
                    self.stubs = {}
                return
            eng = shared_engine()
            request = ""
            recent: List[str] = []
            for m in messages:
                if _is_real_user(m):
                    request = _text(m)
                if m.get("role") in ("user", "assistant") and _text(m).strip():
                    recent.append(f"{m['role']}: {' '.join(_text(m).split())[:300]}")
            ex = int(cfg.get("excerpt_chars", 700))
            budget = int(cfg.get("request_chars", 60000))
            keep_t = float(cfg.get("keep_threshold", 0.5))
            drop: List[Dict[str, Any]] = []
            # Batch under Jev's request budget.
            group: List[Dict[str, Any]] = []
            used = 0
            groups: List[List[Dict[str, Any]]] = []
            for c in cands:
                size = min(len(c["text"]), ex) + 200
                if group and used + size > budget:
                    groups.append(group)
                    group, used = [], 0
                group.append(c)
                used += size
            if group:
                groups.append(group)
            for g in groups:
                items = {}
                qs: Dict[str, Any] = {}
                for i, c in enumerate(g):
                    t = c["text"]
                    body = t if len(t) <= ex else f"{t[: ex - 200]}\n...\n{t[-200:]}"
                    items[f"i{i}"] = {"tool": c["tool"], "what": c["kind"],
                                      "call": _short_args(c["args"]), "chars": len(t), "excerpt": body}
                    qs[f"need_{i}"] = Noul(
                        f"Will `items.i{i}` still be needed, verbatim, to carry out `request` or the work it "
                        "continues? Answer no if it is output or input from an earlier step that is finished, whose "
                        "outcome later messages already reflect, or that could simply be re-read from disk.")
                state = {"request": request[:3000], "recent_conversation": "\n".join(recent[-6:])[-3000:],
                         "items": items}
                d = eng.decide(f"{POINT}.score", state, qs, lambda a: Verdict("scored"),
                               spec_version=SPEC_VERSION, log_detail={"items": len(g), "shadow": shadow})
                if d.error:
                    self.stats["errors"] += 1
                    if not shadow:
                        self.stubs = {}   # fail open: request unchanged
                    return
                for i, c in enumerate(g):
                    if getattr(d.answers[f"need_{i}"], "probability", 1.0) < keep_t:
                        drop.append(c)
            self.stats["decisions"] += 1
            removed = sum(len(c["text"]) for c in drop)
            try:
                from .config import POLICY_VERSION

                eng.store.log(session_id=self.session_id, point=POINT, mode="shadow" if shadow else "enforce",
                              spec_version=SPEC_VERSION, policy_version=POLICY_VERSION, model=eng.client.model,
                              state_hash="", answers_json="{}", action="would_trim" if shadow else "trim",
                              applied=0 if shadow else 1, cached=0, latency_ms=0.0, input_tokens=0, error=None,
                              detail_json=json.dumps({"candidates": len(cands), "dropped": len(drop),
                                                      "chars_removed": removed,
                                                      "chars_considered": sum(len(c["text"]) for c in cands),
                                                      # what was (or would be) dropped, so a later
                                                      # report can check whether the agent needed it again
                                                      "items": [{"key": c["key"], "tool": c["tool"],
                                                                 "call": _short_args(c["args"], 200),
                                                                 "chars": len(c["text"])} for c in drop]}))
            except Exception:
                logger.debug("jermes context_trim log failed", exc_info=True)
            logger.info("jermes context_trim: %d of %d old items %s (%d chars)", len(drop), len(cands),
                        "would be trimmed" if shadow else "trimmed", removed)
            if shadow:
                return
            from .store import data_dir

            directory = data_dir() / "full_results"
            fresh: Dict[str, str] = {}
            for c in drop:
                path = _save(c["text"], directory)
                if c["key"].endswith(":r"):
                    fresh[c["key"]] = stub_result(c["tool"], c["args"], c["text"], path)
                else:
                    new = stub_args(c["text"], path)
                    if new is None:
                        continue
                    fresh[c["key"]] = new
                self.stats["trimmed_items"] += 1
                self.stats["trimmed_chars"] += len(c["text"]) - len(fresh[c["key"]])
            self.stubs = fresh
        except Exception:
            self.stats["errors"] += 1
            if not shadow:
                self.stubs = {}
            logger.debug("jermes context_trim decide failed", exc_info=True)

    # -- applying stubs (every request) --------------------------------------

    def _render(self, messages: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        out: List[Dict[str, Any]] = []
        changed = False
        for m in messages:
            if m.get("role") == "tool":
                s = self.stubs.get(f"{m.get('tool_call_id') or ''}:r")
                if s is not None and m.get("content") != s:
                    m = dict(m)
                    m["content"] = s
                    changed = True
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                tcs = []
                touched = False
                for tc in m["tool_calls"]:
                    cid = tc.get("id") or tc.get("call_id") or ""
                    s = self.stubs.get(f"{cid}:a")
                    f = tc.get("function") or {}
                    if s is not None and f.get("arguments") != s:
                        tc = dict(tc)
                        tc["function"] = {**f, "arguments": s}
                        touched = True
                    tcs.append(tc)
                if touched:
                    m = dict(m)
                    m["tool_calls"] = tcs
                    changed = True
            out.append(m)
        return out if changed else None


# ---------------------------------------------------------------- Hermes engine

def build_engine(hermes_config: Optional[Dict[str, Any]] = None):
    """A ContextEngine for Hermes: its own compressor plus per-request trimming.

    Returns None if this Hermes version lacks the pieces (the plugin then
    simply doesn't register an engine and Hermes keeps its built-in one).
    """
    try:
        import inspect

        from agent.context_compressor import ContextCompressor  # type: ignore
        from agent.context_engine import ContextEngine  # type: ignore
    except Exception:
        return None
    if not hasattr(ContextEngine, "select_context"):
        return None

    class JermesContextEngine(ContextCompressor):
        @property
        def name(self) -> str:  # type: ignore[override]
            return "jermes"

        def _trimmer(self) -> Trimmer:
            sid = self.__dict__.get("_jermes_session_id") or getattr(self, "_session_id", "") or ""
            if sid:
                return trimmer_for(sid)
            t = self.__dict__.get("_jermes_trimmer")   # no session id (tests, one-off agents)
            if t is None:
                t = Trimmer()
                self.__dict__["_jermes_trimmer"] = t
            return t

        def on_session_start(self, session_id: str, **kwargs) -> None:
            self.__dict__["_jermes_session_id"] = session_id or ""
            parent = getattr(super(), "on_session_start", None)
            if callable(parent):
                parent(session_id, **kwargs)

        def select_context(self, request_messages, *, conversation_messages=None, incoming_message=None,
                           budget_tokens=0):
            try:
                return self._trimmer().apply(request_messages)
            except Exception:
                logger.debug("jermes select_context failed", exc_info=True)
                return None

        def on_session_reset(self) -> None:
            sid = self.__dict__.get("_jermes_session_id") or getattr(self, "_session_id", "") or ""
            if sid:
                forget_session(sid)
            self.__dict__.pop("_jermes_trimmer", None)
            parent = getattr(super(), "on_session_reset", None)
            if callable(parent):
                parent()

    comp = (hermes_config or {}).get("compression") or {}

    def _int(key: str, default: int) -> int:
        v = comp.get(key, default)
        try:
            return default if isinstance(v, bool) else int(v)
        except (TypeError, ValueError):
            return default

    # Mirror the settings Hermes passes its built-in compressor, so choosing
    # this engine changes nothing about compaction itself.
    raw_mt = comp.get("model_thresholds") or {}
    cap = comp.get("threshold_tokens")
    try:
        cap = int(cap) if cap is not None and int(cap) > 0 else None
    except (TypeError, ValueError):
        cap = None
    wanted = {
        "model": "",
        "threshold_percent": float(comp.get("threshold", 0.50)),
        "protect_first_n": max(0, _int("protect_first_n", 3)),
        "protect_last_n": _int("protect_last_n", 20),
        "summary_target_ratio": float(comp.get("target_ratio", 0.20)),
        "summary_model_override": None,
        "abort_on_summary_failure": str(comp.get("abort_on_summary_failure", False)).lower() in {"true", "1", "yes"},
        "tail_mode": str(comp.get("tail_mode", "lean")).strip().lower(),
        "min_tail_user_messages": max(1, _int("min_tail_user_messages", 1)),
        "proactive_prune_tokens": max(0, _int("proactive_prune_tokens", 0)),
        "proactive_prune_min_result_chars": _int("proactive_prune_min_result_chars", 8000),
        "proactive_prune_min_reclaim_tokens": max(0, _int("proactive_prune_min_reclaim_tokens", 4096)),
        "model_thresholds": {str(k): float(v) for k, v in raw_mt.items()
                             if isinstance(v, (int, float)) and not isinstance(v, bool)}
                            if isinstance(raw_mt, dict) else {},
        "threshold_tokens_cap": cap,
        "quiet_mode": True,
    }
    params = inspect.signature(ContextCompressor.__init__).parameters
    return JermesContextEngine(**{k: v for k, v in wanted.items() if k in params})
