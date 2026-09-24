"""Hermes hook and middleware callbacks.

Every callback here obeys three rules:

1. Never raise into Hermes. Any failure means "no directive / unchanged".
2. Only change Hermes' behaviour when the point's mode says so:
     shadow  -> ask Jev, log the would-be action, return nothing
     advise  -> advisory output only (notes, suggestions)
     enforce -> may block, escalate, filter, or reroute
3. Stay faster than Hermes' hook timeout. The Jev client has its own deadline,
   so a slow Jev makes ``pre_tool_call`` fail *open* instead of Hermes failing
   it closed.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from .engine import Engine, Verdict
from .points import loop_guard, model_router, result_filter, risk_gate, skill_suggest

logger = logging.getLogger("jermes")


def _ranked(_answers: Dict[str, Any]) -> Verdict:
    return Verdict("ranked")


class Harness:
    def __init__(self, engine: Optional[Engine] = None) -> None:
        self.engine = engine or Engine()
        cfg = self.engine.config["points"]
        self.history = loop_guard.History(window=int(cfg["loop_guard"].get("window", 8)))
        self._lock = threading.Lock()
        self._last_result: Dict[str, str] = {}
        self._route: Dict[str, str] = {}  # session_id -> cheap model for this turn
        self._roster: Optional[list] = None
        # Shadow-mode decisions are observed, never acted on, so they must not
        # add latency to the agent loop: run them off-thread.
        self._shadow_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jermes-shadow")
        self.background_shadow = True

    def _shadow(self, point: str, *args: Any, **kwargs: Any) -> bool:
        """If ``point`` is in shadow mode, evaluate it in the background and return True."""
        if self.engine.mode(point) != "shadow" or not self.background_shadow:
            return False
        try:
            self._shadow_pool.submit(self.engine.decide, point, *args, **kwargs)
        except RuntimeError:  # pool shut down
            pass
        return True

    # ------------------------------------------------------------------ turn

    def on_pre_llm_call(self, session_id: str = "", user_message: str = "", **kw: Any) -> Optional[Dict[str, str]]:
        """Once per user turn. Records the request, routes the model, suggests a skill."""
        try:
            self.history.set_request(session_id, user_message or "")
            with self._lock:
                self._last_result.pop(session_id, None)
                self._route.pop(session_id, None)
            self._route_turn(session_id, user_message or "")
            block = self._suggest_skill(session_id, user_message or "")
            return {"context": block} if block else None
        except Exception:
            logger.debug("jermes pre_llm_call failed", exc_info=True)
            return None

    def _route_turn(self, session_id: str, user_message: str) -> None:
        point = "model_router"
        cfg = self.engine.point_config(point)
        if not self.engine.enabled(point) or not user_message.strip():
            return
        state, qs = model_router.build(user_message)
        policy = model_router.make_policy(cfg)
        kw = {"session_id": session_id, "spec_version": model_router.SPEC_VERSION}
        if self._shadow(point, state, qs, policy, **kw):
            return
        d = self.engine.decide(point, state, qs, policy, **kw)
        cheap = cfg.get("cheap_model")
        if d.enforcing and d.action == "cheap" and cheap:
            with self._lock:
                self._route[session_id] = str(cheap)
            self.engine.mark_applied(d)

    def _suggest_skill(self, session_id: str, user_message: str) -> Optional[str]:
        point = "skill_suggest"
        if not self.engine.enabled(point) or not user_message.strip():
            return None
        if self.engine.mode(point) == "shadow" and self.background_shadow:
            try:
                self._shadow_pool.submit(self.rank_skills, session_id, user_message)
            except RuntimeError:
                pass
            return None
        ranking = self.rank_skills(session_id, user_message)
        if ranking is None or self.engine.mode(point) not in ("advise", "enforce"):
            return None
        return skill_suggest.ranking_block(ranking)

    def roster(self) -> list:
        if self._roster is None:
            self._roster = skill_suggest.load_roster()
        return self._roster

    def rank_skills(self, session_id: str, user_message: str) -> Optional[list]:
        """Two-stage Jev ranking of the skill roster for one request.

        Returns a list of ``{"skill", "p", "fits"}`` dicts (most relevant
        first; empty = "no skill applies"), or ``None`` when Jev could not be
        consulted. Every step is logged regardless of mode.
        """
        point = "skill_suggest"
        cfg = self.engine.point_config(point)
        roster = self.roster()
        if len(roster) < 2 or not (user_message or "").strip():
            return None
        by_name = {s.name: s for s in roster}
        state = skill_suggest.state_for(user_message)

        # Call 1: skim every skill (one Jev request per 255-skill chunk).
        chunk_answers = []
        gate = None
        for qs in skill_suggest.skim_questions(roster):
            d = self.engine.decide(point + ".skim", state, qs, _ranked,
                                   session_id=session_id, spec_version=skill_suggest.SPEC_VERSION)
            if not d.ok:
                return None
            chunk_answers.append(d.answers)
            if gate is None and "gate::prose_suffices" in d.answers:
                gate = skill_suggest.gate_value(d.answers)
        if gate is None:
            return None
        if gate < float(cfg.get("gate_threshold", 0.3)):
            # The turn does not want an action taken. An explicit "nothing
            # applies" counters the skill index's own "err on the side of loading".
            self.engine.store.log(session_id=session_id, point=point, mode=self.engine.mode(point),
                                  spec_version=skill_suggest.SPEC_VERSION, action="none",
                                  detail_json=json.dumps({"reason": "gate", "gate": round(gate, 3)}))
            return []

        # Call 2: rerank the shortlist with full descriptions and SKILL.md excerpts.
        shortlist = [n for n, _ in skill_suggest.merge_rankings(chunk_answers, int(cfg.get("shortlist", 5)))]
        if len(shortlist) < 2:
            return None
        qs = skill_suggest.rerank_questions(shortlist, by_name, int(cfg.get("excerpt_chars", 700)))
        d = self.engine.decide(
            point, state, qs,
            skill_suggest.make_rerank_policy(float(cfg.get("fits_threshold", 0.3)), shortlist,
                                             int(cfg.get("max_ranked", 3))),
            session_id=session_id, spec_version=skill_suggest.SPEC_VERSION,
            log_detail={"gate": round(gate, 3), "shortlist": shortlist},
        )
        if not d.ok:
            return None
        if self.engine.mode(point) in ("advise", "enforce"):
            self.engine.mark_applied(d)
        return list(d.detail.get("ranking") or [])

    # ------------------------------------------------------------------ tools

    def on_pre_tool_call(self, tool_name: str = "", args: Optional[Dict[str, Any]] = None,
                         session_id: str = "", task_id: str = "", **kw: Any) -> Optional[Dict[str, Any]]:
        point = "risk_gate"
        try:
            cfg = self.engine.point_config(point)
            if tool_name not in set(cfg.get("gated_tools") or []) or not self.engine.enabled(point):
                return None
            sid = session_id or task_id
            last = self._last_result.get(sid, "")
            state = risk_gate.build_state(
                tool_name, args or {}, self.history.request(sid),
                last_tool_result=last, max_arg_chars=int(cfg.get("max_arg_chars", 4000)),
            )
            qs = risk_gate.questions(bool(last))
            policy = risk_gate.make_policy(cfg)
            kw = {"session_id": sid, "spec_version": risk_gate.SPEC_VERSION, "log_detail": {"tool": tool_name}}
            if self._shadow(point, state, qs, policy, **kw):
                return None
            d = self.engine.decide(point, state, qs, policy, **kw)
            if not d.enforcing:
                return None  # shadow/advise/error: Hermes' own controls decide
            directive = risk_gate.directive(d.action, d.detail)
            if directive:
                self.engine.mark_applied(d)
            return directive
        except Exception:
            logger.debug("jermes pre_tool_call failed", exc_info=True)
            return None

    def on_transform_tool_result(self, tool_name: str = "", args: Optional[Dict[str, Any]] = None,
                                 result: str = "", session_id: str = "", task_id: str = "",
                                 status: str = "", **kw: Any) -> Optional[str]:
        sid = session_id or task_id
        try:
            if not isinstance(result, str):
                return None
            new = self._filter_result(sid, tool_name, result, status)
            current = new if new is not None else result
            note = self._loop_note(sid, tool_name, args or {}, result, status)
            with self._lock:
                self._last_result[sid] = result[-4000:]
            if note:
                return loop_guard.append_note(current, note)
            return new
        except Exception:
            logger.debug("jermes transform_tool_result failed", exc_info=True)
            return None

    def _filter_result(self, sid: str, tool_name: str, result: str, status: str) -> Optional[str]:
        point = "result_filter"
        cfg = self.engine.point_config(point)
        if tool_name not in set(cfg.get("tools") or []) or not self.engine.enabled(point):
            return None
        if status and status not in ("ok", "success"):
            return None
        if not (int(cfg.get("min_chars", 6000)) <= len(result) <= int(cfg.get("max_chars", 100000))):
            return None
        text, wrapper, key = result_filter.extract_text(result)
        if not text or len(text) < int(cfg.get("min_chars", 6000)) * 0.8:
            return None
        chunks = result_filter.chunk(text, int(cfg.get("max_chunks", 120)))
        if len(chunks) < 3:
            return None
        task = self.history.request(sid)
        if not task:
            return None
        state, qs = result_filter.build(task, tool_name, chunks)
        policy = result_filter.make_policy(cfg, len(chunks))
        kw = {"session_id": sid, "spec_version": result_filter.SPEC_VERSION,
              "log_detail": {"tool": tool_name, "chars": len(result)}}
        if self._shadow(point, state, qs, policy, **kw):
            return None
        d = self.engine.decide(point, state, qs, policy, **kw)
        if not d.enforcing or d.action != "filter":
            return None
        self.engine.mark_applied(d)
        return result_filter.render(chunks, d.detail["kept_idx"], wrapper, key, len(text))

    def _loop_note(self, sid: str, tool_name: str, args: Dict[str, Any], result: str, status: str) -> Optional[str]:
        point = "loop_guard"
        cfg = self.engine.point_config(point)
        n = self.history.add(sid, tool_name, args, result, status)
        if not self.engine.enabled(point) or n < 2:
            return None
        every = max(1, int(cfg.get("completion_every", 3)))
        state, qs = loop_guard.build(self.history.request(sid), self.history.snapshot(sid), n % every == 0)
        policy = loop_guard.make_policy(cfg)
        kw = {"session_id": sid, "spec_version": loop_guard.SPEC_VERSION}
        if self._shadow(point, state, qs, policy, **kw):
            return None
        d = self.engine.decide(point, state, qs, policy, **kw)
        if d.advising and d.action in loop_guard.NOTES:
            self.engine.mark_applied(d)
            return loop_guard.NOTES[d.action]
        return None

    # ------------------------------------------------------------------ LLM

    def llm_request_middleware(self, request: Optional[Dict[str, Any]] = None, session_id: str = "",
                               **kw: Any) -> Optional[Dict[str, Any]]:
        """Swap the model for this turn when the router chose the cheap one."""
        try:
            cheap = self._route.get(session_id)
            if not cheap or not isinstance(request, dict) or "model" not in request:
                return None
            if request.get("model") == cheap:
                return None
            new = dict(request)
            new["model"] = cheap
            return {"request": new, "source": "jermes.model_router"}
        except Exception:
            logger.debug("jermes llm_request middleware failed", exc_info=True)
            return None
