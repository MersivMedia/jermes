"""Shadow replay over a synthetic Hermes state.db (same columns Hermes uses)."""

import json
import sqlite3

import pytest

from jermes import replay
from jermes.harness import Harness
from jermes.points import skill_suggest


def _db(path):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
            timestamp REAL NOT NULL);
    """)
    c.execute("INSERT INTO sessions VALUES ('s1','cli',0)")

    def msg(role, content=None, calls=None):
        c.execute("INSERT INTO messages(session_id,role,content,tool_calls,timestamp) VALUES ('s1',?,?,?,1e10)",
                  (role, content, json.dumps(calls) if calls else None))

    def call(name, args):
        return [{"id": "x", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]

    msg("user", "Please build me a pitch deck as a pptx file for the board")
    msg("assistant", calls=call("skill_view", {"name": "pptx-author"}))
    msg("tool", "skill body")
    msg("assistant", calls=call("skill_view", {"name": "pptx-author", "file_path": "references/x.md"}))
    msg("assistant", calls=call("terminal", {"command": "rm -rf ~/old-decks"}))
    msg("user", "[System note: Your previous turn was interrupted]")  # synthetic, skipped
    msg("user", "explain what a monad is in simple words")
    msg("assistant", "A monad is...")
    msg("user", "ok")  # too short, skipped
    c.commit()
    c.close()


def test_iter_turns_extracts_labels(tmp_path):
    db = tmp_path / "state.db"
    _db(db)
    turns = list(replay.iter_turns(db, limit=10))
    assert [t.request[:12] for t in turns] == ["explain what", "Please build"]  # newest first
    deck = turns[1]
    assert deck.loaded_skills == ["pptx-author"]  # file_path sub-loads are not a skill choice
    assert {"tool": "terminal", "args": {"command": "rm -rf ~/old-decks"}} in deck.tool_calls
    assert turns[0].loaded_skills == []
    assert [t.request[:6] for t in replay.iter_turns(db, only_with_skill=True)] == ["Please"]


def test_iter_turns_is_read_only(tmp_path):
    db = tmp_path / "state.db"
    _db(db)
    before = db.read_bytes()
    list(replay.iter_turns(db))
    assert db.read_bytes() == before


def _ranker(fake):
    def r(qid, q, state):
        req = state.get("request", "") if isinstance(state, dict) else ""
        if qid == "which":
            opts = list(q["criteria"])
            none = skill_suggest.NONE_OPTION
            pick = ("pptx-author" if "pptx-author" in opts else opts[0]) if "pptx" in req else none
            probs = {o: (0.7 if o == pick else 0.3 / (len(opts) - 1)) for o in opts}
            return {"type": "choice", "choice": pick, "probabilities": probs, "confidence": 0.6}
        if qid.startswith("fits::"):
            return {"type": "noul", "noul": 0.8 if "pptx" in qid else 0.1}
        return None

    fake.on(r)


def test_replay_scores_skill_ranking(tmp_path, make_engine, fake):
    db = tmp_path / "state.db"
    _db(db)
    _ranker(fake)
    h = Harness(make_engine(skill_suggest="shadow", risk_gate="shadow"))
    h._roster = [skill_suggest.Skill("pptx-author", "Build decks"), skill_suggest.Skill("powerpoint", "Edit decks"),
                 skill_suggest.Skill("apple-notes", "Notes")]
    out = tmp_path / "disagreements.jsonl"
    report = replay.run(db, limit=10, points="all", harness=h, export=out, progress=lambda *_: None)
    s = report["skills"]
    assert s["turns"] == 2 and s["errors"] == 0
    assert s["top1_agreement_pct"] == 100.0
    assert s["jev_said_none_when_agent_loaded_nothing_pct"] == 100.0
    assert report["risk"]["calls"] == 1
    # Replay ran synchronously even though the point is in shadow mode, and nothing was "applied".
    rows = h.engine.store.recent(50)
    assert rows and all(r["applied"] == 0 for r in rows)
    assert out.exists()


def test_replay_reports_jev_outage(tmp_path, make_engine, fake):
    db = tmp_path / "state.db"
    _db(db)
    fake.status = 403
    fake.error_body = {"error": {"message": "card", "type": "customer_verification_required"}}
    h = Harness(make_engine(skill_suggest="shadow"))
    h._roster = [skill_suggest.Skill("a", "x"), skill_suggest.Skill("b", "y")]
    report = replay.run(db, limit=10, harness=h, progress=lambda *_: None)
    assert report["skills"]["errors"] == 2


def test_replay_missing_db(tmp_path, make_engine):
    with pytest.raises(FileNotFoundError):
        replay.run(tmp_path / "nope.db", harness=Harness(make_engine()), progress=lambda *_: None)


def test_iter_turns_dedupes_compaction_copies(tmp_path):
    db = tmp_path / "state.db"
    _db(db)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO sessions VALUES ('s2','cli',0)")
    c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES ('s2','user',?,1e10)",
              ("explain what a monad is in  simple words",))
    c.commit()
    c.close()
    reqs = [t.request for t in replay.iter_turns(db, limit=10)]
    assert sum("monad" in r for r in reqs) == 1


def test_replay_output_never_carries_secrets(tmp_path, make_engine, fake):
    db = tmp_path / "state.db"
    _db(db)
    key = "vck_" + "Z9y8X7w6V5u4T3s2R1q0PoNmLkJiHgFe"
    c = sqlite3.connect(db)
    c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES ('s1','user',?,1e10)", (f"my key is {key}",))
    c.commit()
    c.close()
    _ranker(fake)
    h = Harness(make_engine(skill_suggest="shadow"))
    h._roster = [skill_suggest.Skill("a", "x"), skill_suggest.Skill("b", "y")]
    lines = []
    out = tmp_path / "export.jsonl"
    report = replay.run(db, limit=10, harness=h, export=out, progress=lines.append)
    blob = "\n".join(lines) + json.dumps(report) + out.read_text() + json.dumps([r for r in fake.requests], default=str)
    assert key not in blob


def test_replay_retries_failed_turns_after_cooldown(tmp_path, make_engine, fake):
    import httpx

    db = tmp_path / "state.db"
    _db(db)
    _ranker(fake)
    state = {"down": True}  # the gateway is down until the cooldown has passed

    def handler(request):
        if state["down"]:
            return httpx.Response(503, json={"error": {"message": "down", "type": "service_unavailable_error"}})
        return fake.handler(request)

    h = Harness(make_engine(skill_suggest="shadow"))
    h._roster = [skill_suggest.Skill("pptx-author", "Build decks"), skill_suggest.Skill("apple-notes", "Notes")]
    h.engine.client._transport = httpx.MockTransport(handler)
    h.engine.client._http = None
    slept = []

    def cooldown(s):
        slept.append(s)
        state["down"] = False

    rep = replay.replay_skills(h, list(replay.iter_turns(db)), sleep=cooldown, progress=None)
    assert slept == [60.0] and rep["errors"] == 0 and rep["turns"] == 2
