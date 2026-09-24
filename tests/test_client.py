import httpx
import pytest

from jermes.client import ClientConfig, JevClient, JevError
from jermes.questions import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    SpecError,
    parse_answer,
    questions_to_wire,
)


def test_spec_limits():
    with pytest.raises(SpecError):
        Choice("x", {"a": None}).to_wire()
    with pytest.raises(SpecError):
        Choice("x", {str(i): None for i in range(256)}).to_wire()
    assert len(Choice("x", {str(i): None for i in range(255)}).to_wire()["criteria"]) == 255
    with pytest.raises(SpecError):
        Score("x", ["one"]).to_wire()
    with pytest.raises(SpecError):
        Score("x", [str(i) for i in range(11)]).to_wire()
    with pytest.raises(SpecError):
        Noul("x", criteria={"yes": "y"}).to_wire()
    with pytest.raises(SpecError):
        questions_to_wire({})


def test_parse_both_dialects():
    assert parse_answer({"type": "noul", "noul": 0.7}) == NoulAnswer(0.7)
    assert parse_answer({"type": "boolean", "probability": 0.2}) == NoulAnswer(0.2)
    c = parse_answer({"type": "choice", "probabilities": {"a": 0.8, "b": 0.2}})
    assert isinstance(c, ChoiceAnswer) and c.choice == "a"
    assert 0.5 < c.confidence < 0.7  # fallback formula: (2*0.8-1)/1 = 0.6
    s = parse_answer({"type": "score", "probabilities": {"0": 0.0, "1": 0.57, "2": 0.43}})
    assert isinstance(s, ScoreAnswer) and abs(s.score - 1.43) < 1e-9


def _client(handler, **kw):
    return JevClient(ClientConfig(backend="vercel", **kw), transport=httpx.MockTransport(handler),
                     sleep=lambda s: None)


def test_vercel_wire_format(fake):
    c = _client(fake.handler, zero_data_retention=True)
    resp = c.ask({"m": "hi"}, {"u": Noul("Is it urgent?")})
    req = fake.requests[0]
    assert req["url"] == "https://ai-gateway.vercel.sh/typesafe/v1/systemone"
    assert req["headers"]["authorization"] == "Bearer test-key"
    assert req["body"]["model"] == "typesafe-ai/jev"
    assert req["body"]["providerOptions"] == {"gateway": {"zeroDataRetention": True}}
    assert resp.answers["u"].noul == 0.1 and resp.input_tokens == 123


def test_typesafe_backend(fake, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
    c = JevClient(ClientConfig(backend="typesafe"), transport=httpx.MockTransport(fake.handler))
    c.ask("s", {"u": Noul("q")})
    req = fake.requests[0]
    assert req["url"] == "https://api.typesafe.ai/v1/systemone"
    assert req["body"]["model"] == "jev-1.13.0"
    assert "providerOptions" not in req["body"]


def test_missing_key(monkeypatch, fake):
    monkeypatch.delenv("AI_GATEWAY_API_KEY")
    c = _client(fake.handler)
    assert not c.available()
    with pytest.raises(JevError) as e:
        c.ask("s", {"u": Noul("q")})
    assert e.value.kind == "config"


def test_retry_then_success():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"})
        return httpx.Response(200, json={"model": "m", "answers": {"u": {"type": "noul", "noul": 0.5}}})

    assert _client(handler).ask("s", {"u": Noul("q")}).answers["u"].noul == 0.5
    assert calls["n"] == 2


def test_error_kind_from_gateway_body():
    def handler(req):
        return httpx.Response(403, json={"error": {"message": "add a card", "type": "customer_verification_required"}})

    with pytest.raises(JevError) as e:
        _client(handler).ask("s", {"u": Noul("q")})
    assert e.value.status == 403 and e.value.kind == "customer_verification_required"


def test_missing_answers_is_error():
    def handler(req):
        return httpx.Response(200, json={"model": "m", "answers": {}})

    with pytest.raises(JevError) as e:
        _client(handler).ask("s", {"u": Noul("q")})
    assert e.value.kind == "parse"


def test_timeout_is_error():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(JevError) as e:
        _client(handler).ask("s", {"u": Noul("q")})
    assert e.value.kind == "timeout"
