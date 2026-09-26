"""Context trimming (context engine) tests with the offline fake Jev."""

from __future__ import annotations

import json

import pytest

from jermes import context_engine as ce

BIG = "line of build output that nobody will read again\n" * 60      # ~3k chars
BIG2 = "def helper():\n    return 42\n" * 120                          # ~3.4k chars


def conv(pause_turn_content="now fix the css on the landing page"):
    """Three user turns. Turn 1 ran a long build and wrote a big file."""
    write_args = json.dumps({"path": "site/index.html", "content": BIG2})
    return [
        {"role": "system", "content": "you are hermes"},
        {"role": "user", "content": "build the site and write the helper"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "npm run build"}'}},
            {"id": "c2", "type": "function", "function": {"name": "write_file", "arguments": write_args}}]},
        {"role": "tool", "tool_call_id": "c1", "content": BIG},
        {"role": "tool", "tool_call_id": "c2", "content": '{"ok": true}'},
        {"role": "assistant", "content": "Built and written."},
        {"role": "user", "content": "deploy it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c3", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "vercel --prod"}'}}]},
        {"role": "tool", "tool_call_id": "c3", "content": "deployed " + "x" * 2000},
        {"role": "assistant", "content": "Deployed."},
        {"role": "user", "content": pause_turn_content},
    ]


@pytest.fixture
def trim(make_engine, fake, monkeypatch, tmp_path):
    eng = make_engine(context_trim="enforce")
    ce.set_shared_engine(eng)
    monkeypatch.setattr("jermes.store.data_dir", lambda: tmp_path)
    yield ce.Trimmer()
    ce.set_shared_engine(None)


def say(fake, need: float):
    fake.on(lambda qid, q, s: {"type": "noul", "noul": need} if qid.startswith("need_") else None)


def test_cold_turn_trims_old_items_and_keeps_recent(trim, fake):
    say(fake, 0.1)
    out = trim.apply(conv(), now=1000.0)
    assert out is not None
    tool1 = next(m for m in out if m.get("tool_call_id") == "c1")
    assert tool1["content"].startswith("[jermes: output of terminal (command=npm run build)")
    path = tool1["content"].split("Full text: ")[1].rstrip("]")
    assert open(path).read() == BIG                                  # full text recoverable
    args = json.loads(out[2]["tool_calls"][1]["function"]["arguments"])
    assert args["path"] == "site/index.html" and "more characters trimmed" in args["content"]
    # turn 2 (the previous turn) is protected by keep_turns=2
    assert next(m for m in out if m.get("tool_call_id") == "c3")["content"].startswith("deployed")
    assert trim.stats["trimmed_items"] == 2


def test_needed_items_are_kept(trim, fake):
    say(fake, 0.9)
    assert trim.apply(conv(), now=1000.0) is None and trim.stats["trimmed_items"] == 0


def test_no_decisions_mid_loop_and_stubs_stable(trim, fake):
    say(fake, 0.1)
    first = trim.apply(conv(), now=1000.0)
    n = len(fake.requests)
    m2 = conv() + [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c4", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.css"}'}}]},
        {"role": "tool", "tool_call_id": "c4", "content": "css " * 1000}]
    second = trim.apply(m2, now=1030.0)                                # 30 s later: warm
    assert len(fake.requests) == n                                      # Jev not asked again
    # the trimmed prefix is byte-identical, so the provider cache still matches
    assert json.dumps(second[: len(first)]) == json.dumps(first)


def test_request_only_original_messages_untouched(trim, fake):
    say(fake, 0.1)
    msgs = conv()
    before = json.dumps(msgs)
    trim.apply(msgs, now=1000.0)
    assert json.dumps(msgs) == before


def test_jev_error_fails_open(trim, fake):
    fake.status = 503
    assert trim.apply(conv(), now=1000.0) is None and trim.stats["errors"] == 1


def test_shadow_never_changes_the_request(make_engine, fake, monkeypatch, tmp_path):
    ce.set_shared_engine(make_engine(context_trim="shadow"))
    monkeypatch.setattr("jermes.store.data_dir", lambda: tmp_path)
    say(fake, 0.1)
    t = ce.Trimmer()
    assert t.apply(conv(), now=1000.0) is None
    ce.set_shared_engine(None)


def test_off_does_nothing(make_engine, fake):
    ce.set_shared_engine(make_engine(context_trim="off"))
    assert ce.Trimmer().apply(conv(), now=1000.0) is None and not fake.requests
    ce.set_shared_engine(None)


def test_protected_tools_and_small_items_are_never_candidates(trim):
    msgs = conv()
    msgs.insert(5, {"role": "assistant", "content": "", "tool_calls": [
        {"id": "m1", "type": "function", "function": {"name": "memory", "arguments": json.dumps({"content": "y" * 5000})}}]})
    msgs.insert(6, {"role": "tool", "tool_call_id": "m1", "content": "z" * 5000})
    keys = {c["key"] for c in trim.candidates(msgs, ce.Trimmer.cfg())}
    assert keys == {"c1:r", "c2:a"}                                     # memory, small results, recent turn excluded


def test_stub_args_keeps_valid_json_and_skips_non_json():
    s = ce.stub_args(json.dumps({"path": "a.py", "content": "q" * 5000}), "/p/full.txt")
    d = json.loads(s)
    assert d["path"] == "a.py" and len(d["content"]) < 400 and "/p/full.txt" in d["content"]
    assert ce.stub_args("not json at all " * 200, "/p") is None
    assert ce.stub_args(json.dumps({"path": "short"}), "/p") is None   # nothing long: no change


def test_build_engine_returns_none_without_hermes(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("agent."):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert ce.build_engine({}) is None


def test_warm_requests_never_trim_new_items(trim, fake):
    # A new old-enough item appears while the cache is warm: it must wait for
    # the next cold turn, or the cached prefix would change mid-loop.
    say(fake, 0.9)                                         # first cold turn: keep everything
    trim.apply(conv(), now=1000.0)
    say(fake, 0.1)                                         # Jev would now drop items...
    later = conv("and the footer too") + [{"role": "user", "content": "one more thing"}]
    assert trim.apply(later, now=1100.0) is None           # ...but 100 s later is warm: no change
    out = trim.apply(later, now=1100.0 + 400)              # after the TTL: cold, trimmed now
    assert out is not None and trim.stats["cold_turns"] == 2


def test_trimmed_item_comes_back_when_a_later_request_needs_it(trim, fake):
    say(fake, 0.1)
    first = trim.apply(conv(), now=1000.0)
    assert next(m for m in first if m.get("tool_call_id") == "c1")["content"].startswith("[jermes")
    say(fake, 0.95)                                           # the next request needs the build log
    later = conv("why did the build warn earlier?")
    out = trim.apply(later, now=2000.0)                       # cold again
    assert out is None or next(m for m in out if m.get("tool_call_id") == "c1")["content"] == BIG
