"""HTTP client for Jev.

Two backends speak the same TypeSafe wire format (``POST {base}/v1/systemone``):

* ``vercel``   - Vercel AI Gateway's TypeSafe-compatible API
                 (base ``https://ai-gateway.vercel.sh/typesafe``,
                 key ``AI_GATEWAY_API_KEY``, model ``typesafe-ai/jev``).
* ``typesafe`` - TypeSafe directly
                 (base ``https://api.typesafe.ai``, key ``TYPESAFE_API_KEY``,
                 model ``jev-1.13.0``).

The client enforces a hard wall-clock deadline per call. Hermes fails a
``pre_tool_call`` hook *closed* when it exceeds ``plugins.hook_callback_timeout``,
so Jermes must always give up first and let the caller fail open.
"""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

import httpx

from . import __version__
from .questions import Answer, Question, parse_answer, questions_to_wire

BACKENDS: Dict[str, Dict[str, str]] = {
    "vercel": {
        "base_url": "https://ai-gateway.vercel.sh/typesafe",
        "api_key_env": "AI_GATEWAY_API_KEY",
        "model": "typesafe-ai/jev",
    },
    "typesafe": {
        "base_url": "https://api.typesafe.ai",
        "api_key_env": "TYPESAFE_API_KEY",
        "model": "jev-1.13.0",
    },
    # OpenRouter serves Jev through its System One API, which implements
    # TypeSafe's request/response shapes (POST {base}/v1/systemone). Jev is not
    # in OpenRouter's chat /models catalog. Docs:
    # https://openrouter.ai/docs/guides/community/typesafe-sdk
    "openrouter": {
        "base_url": "https://openrouter.ai/api",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "typesafe/jev-1.13",
    },
}

RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}


class JevError(RuntimeError):
    """Any failure to obtain answers. Callers treat this as 'no decision'."""

    def __init__(self, message: str, *, status: Optional[int] = None, kind: str = "error"):
        super().__init__(message)
        self.status = status
        self.kind = kind


@dataclass
class JevResponse:
    model: str
    answers: Dict[str, Answer]
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ClientConfig:
    backend: str = "vercel"
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    deadline_s: float = 2.5
    max_retries: int = 3  # bounded by deadline_s, so retries never delay Hermes
    min_interval_s: float = 0.0  # pacing between requests (batch/replay use; live hooks keep 0)
    zero_data_retention: bool = False

    def resolved(self) -> Dict[str, Any]:
        if self.backend not in BACKENDS:
            raise JevError(f"unknown backend {self.backend!r}", kind="config")
        preset = BACKENDS[self.backend]
        key_env = self.api_key_env or preset["api_key_env"]
        return {
            "base_url": (self.base_url or preset["base_url"]).rstrip("/"),
            "model": self.model or preset["model"],
            "api_key": self.api_key or os.environ.get(key_env, ""),
            "api_key_env": key_env,
        }


class JevClient:
    def __init__(
        self,
        config: Optional[ClientConfig] = None,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or ClientConfig()
        self._transport = transport
        self._sleep = sleep
        self._http: Optional[httpx.Client] = None
        self._pace_lock = threading.Lock()
        self._next_slot = 0.0

    def _pace(self) -> None:
        gap = self.config.min_interval_s
        if gap <= 0:
            return
        with self._pace_lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + gap
        if wait > 0:
            self._sleep(wait)

    # -- plumbing ---------------------------------------------------------

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                transport=self._transport,
                headers={"User-Agent": f"jermes/{__version__} (+https://github.com/MersivMedia/jermes)"},
            )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    @property
    def model(self) -> str:
        return self.config.resolved()["model"]

    def available(self) -> bool:
        try:
            return bool(self.config.resolved()["api_key"])
        except JevError:
            return False

    # -- API ----------------------------------------------------------------

    def build_payload(self, state: Any, questions: Mapping[str, Question]) -> Dict[str, Any]:
        cfg = self.config.resolved()
        payload: Dict[str, Any] = {
            "model": cfg["model"],
            "state": state,
            "questions": questions_to_wire(questions),
        }
        if self.config.backend == "vercel" and self.config.zero_data_retention:
            payload["providerOptions"] = {"gateway": {"zeroDataRetention": True}}
        return payload

    def ask(self, state: Any, questions: Mapping[str, Question]) -> JevResponse:
        cfg = self.config.resolved()
        if not cfg["api_key"]:
            raise JevError(f"{cfg['api_key_env']} is not set", kind="config")
        payload = self.build_payload(state, questions)
        url = f"{cfg['base_url']}/v1/systemone"
        headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}

        deadline = time.monotonic() + self.config.deadline_s
        start: Optional[float] = None
        attempt = 0
        while True:
            self._pace()
            if start is None:
                start = time.monotonic()  # latency excludes the pacing wait (batch jobs only)
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                raise JevError("deadline exceeded", kind="timeout")
            try:
                resp = self._client().post(url, json=payload, headers=headers, timeout=remaining)
            except httpx.TimeoutException as exc:
                raise JevError(f"timeout: {exc}", kind="timeout") from exc
            except httpx.HTTPError as exc:
                raise JevError(f"transport error: {exc}", kind="transport") from exc

            if resp.status_code == 200:
                break
            if resp.status_code in RETRYABLE_STATUS and attempt < self.config.max_retries:
                attempt += 1
                backoff = _retry_after(resp) or (0.15 * (2 ** attempt) + random.uniform(0, 0.05))
                if time.monotonic() + backoff >= deadline:
                    raise JevError(f"HTTP {resp.status_code}; no time to retry", status=resp.status_code,
                                   kind=_error_kind(resp))
                self._sleep(backoff)
                continue
            raise JevError(_error_message(resp), status=resp.status_code, kind=_error_kind(resp))

        latency_ms = (time.monotonic() - (start or time.monotonic())) * 1000.0
        try:
            body = resp.json()
            answers = {qid: parse_answer(a) for qid, a in (body.get("answers") or {}).items()}
        except Exception as exc:  # malformed body = no decision
            raise JevError(f"unparseable response: {exc}", kind="parse") from exc
        missing = set(payload["questions"]) - set(answers)
        if missing:
            raise JevError(f"response missing answers for {sorted(missing)}", kind="parse")
        usage = body.get("usage") or {}
        return JevResponse(
            model=str(body.get("model") or cfg["model"]),
            answers=answers,
            input_tokens=int(usage.get("input_tokens", usage.get("inputTokens", 0)) or 0),
            output_tokens=int(usage.get("output_tokens", usage.get("outputTokens", 0)) or 0),
            latency_ms=latency_ms,
            raw=body,
        )


def _retry_after(resp: httpx.Response) -> Optional[float]:
    value = resp.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
        if isinstance(err, dict):
            return f"HTTP {resp.status_code}: {err.get('message') or err}"
        if err:
            return f"HTTP {resp.status_code}: {err}"
    except Exception:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:200]}"


def _error_kind(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
        if isinstance(err, dict) and err.get("type"):
            return str(err["type"])
    except Exception:
        pass
    return {401: "auth", 403: "forbidden", 422: "invalid_request", 429: "rate_limited"}.get(
        resp.status_code, "http"
    )
