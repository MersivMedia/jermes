"""trimreport: classify whether trimmed items were needed again."""

from __future__ import annotations

import json
import sqlite3

from jermes import trimreport as tr


def _state(tmp_path, calls):
    """calls: list of (ts, tool, args, result_text) after the trim decision."""
    db = tmp_path / "state.db"
    c = sqlite3.connect(db)
    c.execute("create table messages (id integer primary key, session_id text, role text, content text, "
              "tool_call_id text, tool_calls text, timestamp real)")
    for i, (ts, tool, args, result) in enumerate(calls):
        cid = f"c{i}"
        c.execute("insert into messages (session_id, role, content, tool_call_id, tool_calls, timestamp) "
                  "values ('s', 'assistant', '', null, ?, ?)",
                  (json.dumps([{"id": cid, "function": {"name": tool, "arguments": json.dumps(args)}}]), ts))
        c.execute("insert into messages (session_id, role, content, tool_call_id, tool_calls, timestamp) "
                  "values ('s', 'tool', ?, ?, null, ?)", (result, cid, ts))
    # something *before* the decision that must be ignored
    c.execute("insert into messages (session_id, role, content, tool_call_id, tool_calls, timestamp) "
              "values ('s', 'assistant', '', null, ?, 50)",
              (json.dumps([{"id": "early", "function": {"name": "read_file",
                                                        "arguments": json.dumps({"path": "spec.md"})}}]),))
    c.commit()
    return db


def _decisions(tmp_path, items, action="would_trim"):
    db = tmp_path / "decisions.sqlite"
    c = sqlite3.connect(db)
    c.execute("create table decisions (id integer primary key, session_id text, ts real, point text, "
              "action text, detail_json text)")
    c.execute("insert into decisions (session_id, ts, point, action, detail_json) values ('s', 100, "
              "'context_trim', ?, ?)", (action, json.dumps({"items": items})))
    c.commit()
    return db


ITEMS = [
    {"tool": "read_file", "call": "path=logs/app.log", "chars": 90000},
    {"tool": "read_file", "call": "path=spec.md", "chars": 60000},
    {"tool": "terminal", "call": "command=npm run build", "chars": 8000},
    {"tool": "skill_view", "call": "name=comfyui", "chars": 26000},
    {"tool": "read_file", "call": "path=users.json", "chars": 40000},
]


def run(tmp_path, calls, items=ITEMS, action="would_trim"):
    return tr.report(_decisions(tmp_path, items, action), _state(tmp_path, calls))


def outcome(r, call):
    return next(d["outcome"] for d in r["detail"] if d["call"] == call)


def test_each_way_of_going_back_is_classified(tmp_path):
    r = run(tmp_path, [
        (110, "search_files", {"pattern": "session-cache", "path": "logs/app.log"}, "278|resized to 8192"),
        (120, "read_file", {"path": "./spec.md"}, "x" * 60000),
        (130, "terminal", {"command": "npm  run build"}, "ok"),
        (140, "skill_view", {"name": "comfyui"}, "y" * 26000),
    ])
    assert outcome(r, "path=logs/app.log") == "searched"
    assert outcome(r, "path=spec.md") == "re-read"
    assert outcome(r, "command=npm run build") == "re-ran"
    assert outcome(r, "name=comfyui") == "reloaded"
    assert outcome(r, "path=users.json") == "not needed"
    assert r["needed_rate"] == 0.8 and r["shadow_items"] == 5


def test_grep_in_terminal_and_ranged_read_count_as_searches(tmp_path):
    r = run(tmp_path, [
        (110, "terminal", {"command": "grep -n 'Enterprise' spec.md | head -5"}, "412|417 days"),
        (120, "read_file", {"path": "logs/app.log", "offset": 270, "limit": 20}, "z" * 1500),
    ])
    assert outcome(r, "path=spec.md") == "searched"
    assert outcome(r, "path=logs/app.log") == "searched"
    assert r["chars_of_items_fully_reloaded"] == 0


def test_calls_before_the_decision_and_other_files_do_not_count(tmp_path):
    r = run(tmp_path, [
        (110, "read_file", {"path": "other/spec.md.bak"}, "q"),
        (120, "terminal", {"command": "npm run build --watch"}, "q"),
        (130, "search_files", {"pattern": "x", "path": "logs"}, "q"),
    ])
    assert r["outcomes"] == {"not needed": 5}                 # the early read at ts=50 is ignored too


def test_opening_the_saved_full_copy_is_recorded(tmp_path):
    r = run(tmp_path, [(110, "read_file", {"path": "/h/jermes/full_results/9f2c.txt"}, "w" * 90000)],
            items=[ITEMS[0]], action="trim")
    assert outcome(r, "path=logs/app.log") == "full copy opened"
    assert r["shadow_items"] == 0


def test_empty_log_renders_guidance(tmp_path):
    r = run(tmp_path, [], items=[])
    assert r["items"] == 0 and "context: {engine: jermes}" in tr.render(r)


def test_render_lists_items_went_back_for(tmp_path):
    r = run(tmp_path, [(120, "read_file", {"path": "spec.md"}, "x" * 60000)])
    text = tr.render(r)
    assert "Needed again (any way): 20%" in text and "re-read" in text and "spec.md" in text


def test_item_trimmed_at_two_cold_turns_counts_once(tmp_path):
    db = tmp_path / "decisions.sqlite"
    c = sqlite3.connect(db)
    c.execute("create table decisions (id integer primary key, session_id text, ts real, point text, "
              "action text, detail_json text)")
    for ts in (100, 400):
        c.execute("insert into decisions (session_id, ts, point, action, detail_json) values ('s', ?, "
                  "'context_trim', 'trim', ?)", (ts, json.dumps({"items": [ITEMS[1]]})))
    c.commit()
    r = tr.report(db, _state(tmp_path, [(500, "search_files", {"pattern": "x", "path": "spec.md"}, "hit")]))
    assert r["items"] == 1 and r["outcomes"] == {"searched": 1}
