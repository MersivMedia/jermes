"""Model adapters for ingestion: strong-model escalation (I7) and baselines.

Talks to any OpenAI-compatible chat endpoint. Defaults to the Vercel AI
Gateway because that's where the Jev key already lives; any gateway model id
works (``anthropic/claude-sonnet-4.5``, ``openai/gpt-5.4-mini``, ...).

Every call records its real token usage and cost, so the pipeline and the
baselines are compared on what they actually spent.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

GATEWAY_URL = "https://ai-gateway.vercel.sh/v1"
ANTHROPIC_URL = "https://api.anthropic.com/v1"

# Anthropic list prices, $ per million tokens (input, output), from
# https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-09-25).
ANTHROPIC_PRICES = {
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
}


@dataclass
class Spend:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    by_model: Dict[str, Dict[str, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, model: str, tin: int, tout: int, usd: float) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += tin
            self.output_tokens += tout
            self.usd += usd
            m = self.by_model.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0})
            m["calls"] += 1
            m["input_tokens"] += tin
            m["output_tokens"] += tout
            m["usd"] += usd

    def as_dict(self) -> Dict[str, Any]:
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "usd": round(self.usd, 5), "by_model": self.by_model}


class ChatModel:
    """Minimal chat client with list-price accounting.

    ``anthropic:<model>`` talks to the Anthropic Messages API with
    ANTHROPIC_API_KEY; anything else goes to an OpenAI-compatible endpoint
    (the Vercel AI Gateway by default).
    """

    _prices: Dict[str, tuple] = {}

    def __init__(self, model: str, *, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 spend: Optional[Spend] = None, timeout: float = 120.0, transport=None) -> None:
        self.anthropic = model.startswith("anthropic:")
        self.model = model.split(":", 1)[1] if self.anthropic else model
        if self.anthropic:
            self.base_url = (base_url or ANTHROPIC_URL).rstrip("/")
            self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        else:
            self.base_url = (base_url or os.environ.get("JERMES_LLM_BASE_URL") or GATEWAY_URL).rstrip("/")
            self.api_key = api_key or os.environ.get("JERMES_LLM_API_KEY") or os.environ.get("AI_GATEWAY_API_KEY", "")
        self.label = model
        self.spend = spend if spend is not None else Spend()
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def price(self) -> tuple:
        """(input $/token, output $/token)."""
        if self.anthropic:
            base = re.sub(r"-\d{8}$", "", self.model)
            pin, pout = ANTHROPIC_PRICES.get(base, (0.0, 0.0))
            return pin / 1e6, pout / 1e6
        if self.model not in ChatModel._prices:
            try:
                r = self._http.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
                for m in r.json().get("data", []):
                    p = m.get("pricing") or {}
                    if "input" in p and "output" in p:
                        ChatModel._prices[m["id"]] = (float(p["input"]), float(p["output"]))
            except Exception:
                pass
            ChatModel._prices.setdefault(self.model, (0.0, 0.0))
        return ChatModel._prices[self.model]

    def _request(self, system: str, user: str, max_tokens: int) -> tuple:
        if self.anthropic:
            r = self._http.post(f"{self.base_url}/messages", headers={
                "x-api-key": self.api_key, "anthropic-version": "2023-06-01"}, json={
                "model": self.model, "max_tokens": max_tokens, "temperature": 0, "system": system,
                "messages": [{"role": "user", "content": user}]})
            if r.status_code >= 400:
                return r, None
            d = r.json()
            u = d.get("usage") or {}
            text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
            return r, (text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)))
        r = self._http.post(f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"},
                            json={"model": self.model, "max_tokens": max_tokens, "temperature": 0,
                                  "messages": [{"role": "system", "content": system},
                                               {"role": "user", "content": user}]})
        if r.status_code >= 400:
            return r, None
        d = r.json()
        u = d.get("usage") or {}
        return r, ((d["choices"][0]["message"].get("content") or ""), int(u.get("prompt_tokens", 0)),
                   int(u.get("completion_tokens", 0)))

    def complete(self, system: str, user: str, *, max_tokens: int = 800) -> str:
        import time as _t

        last: Optional[str] = None
        for attempt in range(5):
            try:
                r, out = self._request(system, user, max_tokens)
            except httpx.HTTPError as exc:
                last = str(exc)
                _t.sleep(2 * (attempt + 1))
                continue
            if out is None:
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code in (429, 500, 502, 503, 504, 529):
                    _t.sleep(float(r.headers.get("retry-after") or 3 * (attempt + 1)))
                    continue
                break
            text, tin, tout = out
            pin, pout = self.price()
            self.spend.add(self.label, tin, tout, tin * pin + tout * pout)
            return text.strip()
        raise RuntimeError(f"{self.label}: {last}")


def _json_obj(text: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


EXTRACT_SYSTEM = ("You extract values from documents. Copy each value exactly as it appears in the document "
                  "text, character for character. If the document does not state a value, use null. "
                  "Reply with one JSON object and nothing else.")


def extract_all(model: ChatModel, fields: List[Any], text: str) -> Dict[str, Optional[str]]:
    """Baseline: one model reads the whole document and fills every field."""
    spec = "\n".join(f'- "{f.name}": {f.question}' for f in fields)
    out = _json_obj(model.complete(EXTRACT_SYSTEM, f"Fields:\n{spec}\n\nDocument:\n{text}",
                                   max_tokens=150 + 60 * len(fields)))
    return {f.name: (str(out[f.name]) if out.get(f.name) not in (None, "") else None) for f in fields}


def extractor(model: ChatModel):
    """An I7 escalation function: re-extract the flagged fields with a strong model, one call."""
    def _flagged(fields, text: str) -> Dict[str, Optional[str]]:
        return extract_all(model, list(fields), text)
    return _flagged


def proposer(model: ChatModel):
    """An I4 generator for fields no regex can find (names, free text)."""
    def _propose(f, text: str) -> List[str]:
        sysmsg = ("List every span in the document that could plausibly answer the question, copied exactly. "
                  'Reply with JSON: {"candidates": [..]} with up to 12 items.')
        out = _json_obj(model.complete(sysmsg, f"Question: {f.question}\n\nDocument:\n{text}", max_tokens=400))
        c = out.get("candidates") or []
        return [str(x) for x in c if isinstance(x, (str, int, float))]
    return _propose
