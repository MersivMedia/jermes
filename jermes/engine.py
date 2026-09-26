"""The decision engine: one code path every decision point goes through.

    state + questions
        -> redact
        -> cache lookup (hit = replay, deterministic)
        -> Jev (bounded by the client deadline)
        -> policy function (pure code, owns the action)
        -> log (always)
        -> Decision(action, applied?)

A decision point never raises into Hermes. Any failure becomes a Decision with
``error`` set and ``action=None``, and the caller does whatever Hermes would
have done without Jermes (fail open), except where a point documents otherwise.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

from .client import ClientConfig, JevClient, JevError, JevResponse
from .config import POLICY_VERSION, load_config
from .questions import Answer, Question, answer_to_dict, parse_answer, questions_to_wire
from .store import Store, cache_key, state_hash

logger = logging.getLogger("jermes")

Policy = Callable[[Dict[str, Answer]], "Verdict"]


@dataclass
class Verdict:
    """What a policy decided. ``action`` is a short stable label for logs."""

    action: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    point: str
    mode: str
    action: Optional[str]
    answers: Dict[str, Answer] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    error: Optional[str] = None
    latency_ms: float = 0.0
    log_id: int = 0
    input_tokens: int = 0  # Jev input tokens billed for this decision (0 on a cache hit)

    @property
    def ok(self) -> bool:
        return self.error is None and self.action is not None

    @property
    def enforcing(self) -> bool:
        return self.ok and self.mode == "enforce"

    @property
    def advising(self) -> bool:
        return self.ok and self.mode in ("advise", "enforce")


_TOKEN_CANDIDATE = re.compile(r"[A-Za-z0-9_\-]{24,}")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")


def _looks_like_secret(tok: str) -> bool:
    """Generic credential shape, independent of any provider's key prefix.

    Hermes' redactor matches known prefixes and missed Vercel's ``vck_`` keys,
    so this catches the class: a long unbroken token with a high-entropy run
    (16+ alphanumerics containing at least two digits and a letter). Snake-case
    identifiers ("generative_media_pipeline_design"), UUIDs and session ids
    have no such run and pass through.
    """
    for run in _ALNUM_RUN.findall(tok):
        if len(run) >= 16 and sum(ch.isdigit() for ch in run) >= 2 and any(ch.isalpha() for ch in run):
            return True
    return False


def _redact_generic(text: str) -> str:
    return _TOKEN_CANDIDATE.sub(
        lambda m: (m.group(0)[:4] + "...[REDACTED]") if _looks_like_secret(m.group(0)) else m.group(0), text
    )


def _redact(value: Any) -> Any:
    try:
        from agent.redact import redact_sensitive_text  # type: ignore
    except Exception:  # standalone install: the generic layer still runs
        redact_sensitive_text = None

    def one(s: str) -> str:
        if redact_sensitive_text is not None:
            try:
                s = redact_sensitive_text(s, force=True)
            except TypeError:  # older signature without force=
                s = redact_sensitive_text(s)
        return _redact_generic(s)

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return one(v)
        if isinstance(v, Mapping):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        return v

    return walk(value)


class Engine:
    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        client: Optional[JevClient] = None,
        store: Optional[Store] = None,
    ) -> None:
        self.config = config or load_config()
        b = self.config["backend"]
        self.client = client or JevClient(
            ClientConfig(
                backend=b.get("name", "vercel"),
                base_url=b.get("base_url"),
                model=b.get("model"),
                deadline_s=float(b.get("deadline_s", 2.5)),
                max_retries=int(b.get("max_retries", 3)),
                zero_data_retention=bool(b.get("zero_data_retention", False)),
            )
        )
        self._store = store
        self._store_lock = threading.Lock()

    @property
    def store(self) -> Store:
        with self._store_lock:
            if self._store is None:
                self._store = Store()
            return self._store

    def point_config(self, point: str) -> Dict[str, Any]:
        # Sub-steps ("skill_suggest.skim") share their parent point's config.
        return self.config["points"].get(point.split(".", 1)[0], {"mode": "off"})

    def mode(self, point: str) -> str:
        return str(self.point_config(point).get("mode", "off"))

    def enabled(self, point: str) -> bool:
        return self.mode(point) != "off" and self.client.available()

    # -- core ---------------------------------------------------------------

    def _ask_cached(self, state: Any, questions: Mapping[str, Question]) -> tuple:
        wire = questions_to_wire(questions)
        model = self.client.model
        key = cache_key(model, state, wire)
        hit = self.store.cache_get(key)
        if hit is not None:
            answers = {qid: parse_answer(a) for qid, a in hit["answers"].items()}
            return answers, hit.get("model", model), True, 0.0, 0
        resp: JevResponse = self.client.ask(state, questions)
        self.store.cache_put(
            key,
            resp.model,
            {"model": resp.model, "answers": {k: answer_to_dict(v) for k, v in resp.answers.items()}},
        )
        return resp.answers, resp.model, False, resp.latency_ms, resp.input_tokens

    def decide(
        self,
        point: str,
        state: Any,
        questions: Mapping[str, Question],
        policy: Policy,
        *,
        session_id: str = "",
        spec_version: str = "1",
        log_detail: Optional[Dict[str, Any]] = None,
    ) -> Decision:
        mode = self.mode(point)
        if mode == "off":
            return Decision(point=point, mode=mode, action=None, error="off")
        if self.config.get("redact", True):
            state = _redact(state)

        answers: Dict[str, Answer] = {}
        model = self.client.model if self.client.available() else ""
        cached, latency, tokens = False, 0.0, 0
        verdict: Optional[Verdict] = None
        error: Optional[str] = None
        try:
            answers, model, cached, latency, tokens = self._ask_cached(state, questions)
            verdict = policy(answers)
        except JevError as exc:
            error = f"{exc.kind}: {exc}"
        except Exception as exc:  # policy bug or spec error: never propagate
            error = f"internal: {type(exc).__name__}: {exc}"
            logger.debug("jermes %s failed", point, exc_info=True)

        decision = Decision(
            point=point,
            mode=mode,
            action=verdict.action if verdict else None,
            answers=answers,
            detail=verdict.detail if verdict else {},
            cached=cached,
            error=error,
            latency_ms=latency,
            input_tokens=0 if cached else int(tokens or 0),
        )
        try:
            detail = dict(log_detail or {})
            detail.update(decision.detail)
            decision.log_id = self.store.log(
                session_id=session_id,
                point=point,
                mode=mode,
                spec_version=spec_version,
                policy_version=POLICY_VERSION,
                model=model,
                state_hash=state_hash(state),
                answers_json=json.dumps({k: answer_to_dict(v) for k, v in answers.items()}),
                action=decision.action,
                applied=0,
                cached=int(cached),
                latency_ms=latency,
                input_tokens=tokens,
                error=error,
                detail_json=json.dumps(detail, default=str)[:20000],
            )
        except Exception:
            logger.debug("jermes log write failed", exc_info=True)
        return decision

    def mark_applied(self, decision: Decision) -> None:
        """Record that Hermes' behaviour actually changed because of a decision."""
        if not decision.log_id:
            return
        try:
            self.store.mark_applied(decision.log_id)
        except Exception:
            logger.debug("jermes mark_applied failed", exc_info=True)
