"""Savings estimate on a synthetic state.db where the answer is known."""

import json
import sqlite3

import pytest

from jermes import labels, savings
from jermes.harness import Harness

PARA = "Relevant fact about the deployment. " * 12
JUNK = "navigation menu cookie banner footer " * 12


def _db(path):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, api_call_count INTEGER,
            message_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, end_reason TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
            tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL);
    """)
    # s1: trustworthy counters, 10 API calls over 10 assistant messages.
    c.execute("INSERT INTO sessions VALUES ('s1','cli','test-model',0,10,20,1000,500,90000,10000,'agent_close')")
    # s0: old-style row with no API-call count; must be excluded.
    c.execute("INSERT INTO sessions VALUES ('s0','telegram','test-model',0,0,5,999999999,0,0,0,NULL)")

    def msg(role, content=None, tool_name=None, call_id=None, calls=None, sid="s1"):
        c.execute("INSERT INTO messages(session_id,role,content,tool_call_id,tool_calls,tool_name,timestamp) "
                  "VALUES (?,?,?,?,?,?,1)", (sid, role, content, call_id, json.dumps(calls) if calls else None, tool_name))
        return c.execute("select last_insert_rowid()").fetchone()[0]

    msg("user", "find the deployment facts in this page")
    msg("assistant", calls=[{"id": "t1", "function": {"name": "web_extract", "arguments": "{}"}}])
    body = "\n\n".join([PARA, JUNK, JUNK, PARA, JUNK, JUNK, JUNK, JUNK])
    big = msg("tool", json.dumps({"success": True, "content": body}), tool_name=None, call_id="t1")
    for _ in range(4):
        msg("assistant", "working")
    msg("user", "thanks, now summarise it")
    skill = msg("tool", json.dumps({"success": True, "name": "apple-notes", "content": "x" * 4000}),
                tool_name="skill_view", call_id="t2")
    for _ in range(5):
        msg("assistant", "ok")
    msg("tool", json.dumps({"content": "y" * 20000}), tool_name="web_extract", sid="s0")  # excluded session
    c.commit()
    c.close()
    return big, skill


def _keep_relevant(fake):
    def r(qid, q, state):
        if qid.startswith("keep_"):
            text = state["sections"][f"s{qid.split('_')[1]}"]
            return {"type": "noul", "noul": 0.9 if "Relevant" in text else 0.05}
        return None
    fake.on(r)


def test_result_filter_estimate_matches_hand_count(tmp_path, make_engine, fake):
    db = tmp_path / "state.db"
    big_id, _ = _db(db)
    _keep_relevant(fake)
    h = Harness(make_engine(result_filter="shadow"))
    h.engine.config["points"]["result_filter"]["min_chars"] = 1000
    h.engine.config["points"]["result_filter"]["tools"] = ["web_extract"]
    conn = savings.open_db(db)
    sessions = savings.real_sessions(conn)
    assert list(sessions) == ["s1"]  # old counter-less session excluded
    rem, stats = savings.estimate_result_filter(h, conn, sessions, progress=None)
    assert stats["big_results"] == 2 and stats["eligible"] == 1 and stats["filtered"] == 1
    r = rem[0]
    assert r.tool == "web_extract"  # name recovered from the assistant's tool_calls via tool_call_id
    # 10 API calls, 10 assistant messages, 9 after the result -> 9 re-reads.
    assert r.later_calls == 9
    assert r.removed_tokens > 0 and r.total_tokens == pytest.approx(r.removed_tokens * 10)
    p = savings.Prices(5, 25, 0.5, 6.25, "t")
    usd = savings.cost(rem, sessions, {"test-model": p})
    assert usd == pytest.approx(r.removed_tokens * 6.25 / 1e6 + r.removed_tokens * 9 * 0.5 / 1e6)


def test_skill_load_estimate_uses_labels(tmp_path):
    db = tmp_path / "state.db"
    _, skill_id = _db(db)
    lf = tmp_path / "labels.jsonl"
    conn = savings.open_db(db)
    user_id = conn.execute("select id from messages where content='thanks, now summarise it'").fetchone()[0]
    labels.append_label(labels.Label(labels.turn_key("s1", user_id), "r", []), lf)  # label: no skill needed
    sessions = savings.real_sessions(conn)
    rem, stats = savings.estimate_skill_loads(conn, sessions, lf)
    assert stats["loads"] == 1 and stats["avoidable"] == 1
    assert rem[0].removed_tokens == pytest.approx(len(conn.execute(
        "select content from messages where id=?", (skill_id,)).fetchone()[0]) / 4)
    labels.append_label(labels.Label(labels.turn_key("s1", user_id), "r", ["apple-notes"]), lf)
    rem2, stats2 = savings.estimate_skill_loads(conn, sessions, lf)
    assert stats2["needed"] == 1 and not rem2


def test_full_run_report(tmp_path, make_engine, fake, capsys):
    db = tmp_path / "state.db"
    _db(db)
    _keep_relevant(fake)
    h = Harness(make_engine(result_filter="shadow"))
    h.engine.config["points"]["result_filter"].update(min_chars=1000, tools=["web_extract"])
    rep = savings.run(db, h, labels_file=tmp_path / "none.jsonl",
                      fallback=savings.Prices(5, 25, 0.5, 6.25, "assumed"), progress=lambda *_: None)
    assert rep["baseline"]["prompt_tokens"] == 101000  # s1 only
    assert rep["result_filter"]["usd"] > 0 and rep["result_filter"]["jev_tokens"] > 0
    savings.print_report(rep)
    assert "result_filter" in capsys.readouterr().out
