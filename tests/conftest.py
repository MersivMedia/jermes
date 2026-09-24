"""Shared fixtures: an offline fake Jev served through httpx.MockTransport."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

import httpx
import pytest

from jermes.client import ClientConfig, JevClient
from jermes.config import DEFAULTS, _deep_merge
from jermes.engine import Engine
from jermes.store import Store


class FakeJev:
    """Answers each question via a responder(qid, question, state) -> raw answer dict.

    Default responder: noul 0.1, choice picks the first option, score picks level 0.
    """

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.responders: List[Callable[[str, Dict[str, Any], Any], Any]] = []
        self.status = 200
        self.error_body: Dict[str, Any] = {}

    def on(self, fn: Callable[[str, Dict[str, Any], Any], Any]) -> None:
        self.responders.insert(0, fn)

    def _answer(self, qid: str, q: Dict[str, Any], state: Any) -> Dict[str, Any]:
        for r in self.responders:
            out = r(qid, q, state)
            if out is not None:
                return out
        if q["type"] == "noul":
            return {"type": "noul", "noul": 0.1}
        if q["type"] == "choice":
            opts = list(q["criteria"])
            probs = {o: (0.9 if i == 0 else 0.1 / max(1, len(opts) - 1)) for i, o in enumerate(opts)}
            return {"type": "choice", "choice": opts[0], "probabilities": probs, "confidence": 0.85}
        levels = len(q["criteria"])
        probs = {str(i): (1.0 if i == 0 else 0.0) for i in range(levels)}
        return {"type": "score", "score": 0.0, "probabilities": probs, "confidence": 0.95}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        if self.status != 200:
            return httpx.Response(self.status, json=self.error_body or {"error": {"message": "nope"}})
        answers = {qid: self._answer(qid, q, body["state"]) for qid, q in body["questions"].items()}
        return httpx.Response(200, json={"model": body["model"], "answers": answers,
                                         "usage": {"input_tokens": 123, "output_tokens": 7}})


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("JERMES_HOME", str(tmp_path / "jermes"))
    monkeypatch.delenv("JERMES_MODE", raising=False)
    monkeypatch.delenv("JERMES_BACKEND", raising=False)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")


@pytest.fixture
def fake() -> FakeJev:
    return FakeJev()


@pytest.fixture
def make_engine(fake, tmp_path):
    def _make(**point_modes: str) -> Engine:
        cfg = _deep_merge(DEFAULTS, {"points": {p: {"mode": m} for p, m in point_modes.items()}})
        client = JevClient(ClientConfig(backend="vercel", deadline_s=2.0),
                           transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
        return Engine(cfg, client=client, store=Store(tmp_path / "decisions.sqlite"))

    return _make
