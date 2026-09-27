"""skill_overlap: create check and duplicate audit, with the offline fake Jev."""

from __future__ import annotations

import json

import pytest

from jermes import skill_audit
from jermes.harness import Harness
from jermes.points import skill_overlap as so
from jermes.points.skill_suggest import Skill

ROSTER = [
    Skill("pdf", "Create, read, merge, fill, and secure PDF files.", "Use pypdf and reportlab..."),
    Skill("styled-pdf-deliverables", "Markdown to a styled client-ready PDF deliverable.", "Render markdown..."),
    Skill("ocr-and-documents", "Extract text from PDFs/scans.", "pymupdf, marker-pdf..."),
    Skill("spotify", "Spotify playback and playlists.", "Use the Web API..."),
]

NEW_SKILL = """---
name: markdown-report-pdf
description: Turn a markdown report into a branded PDF.
---
# Markdown report PDF
Render markdown to HTML, apply the brand CSS, print to PDF with Chrome.
"""


def jev(fake, shortlist: dict, judged: dict):
    """shortlist: name -> prob. judged: name -> (relation, same_job, one_serves_both)."""
    def respond(qid, q, state):
        if qid == "which":
            opts = list(q["criteria"])
            probs = {o: shortlist.get(o, 0.0) for o in opts}
            probs[so.NONE_OPTION] = max(0.0, 1 - sum(probs.values()))
            best = max(probs, key=probs.get)
            return {"type": "choice", "choice": best, "probabilities": probs, "confidence": probs[best]}
        kind, i = qid.split("_")
        name = state["existing"][f"e{i}"]["name"]
        rel, same, one = judged.get(name, ("unrelated", 0.05, 0.05))
        if kind == "rel":
            return {"type": "choice", "choice": rel, "probabilities": {rel: 0.9}, "confidence": 0.9}
        return {"type": "noul", "noul": same if kind == "same" else one}
    fake.on(respond)


def harness(make_engine, mode="advise"):
    h = Harness(make_engine(skill_overlap=mode, risk_gate="off", skill_suggest="off"))
    h._roster = list(ROSTER)
    return h


def create_args(name="markdown-report-pdf", content=NEW_SKILL):
    return {"action": "create", "name": name, "content": content, "category": "productivity"}


def test_parses_create_content():
    t = so.target_from_create(create_args())
    assert t.name == "markdown-report-pdf" and t.description.startswith("Turn a markdown")
    assert t.body.startswith("# Markdown report PDF")
    assert so.target_from_create({"action": "patch", "name": "x", "content": "y"}) is None
    assert so.target_from_create({"action": "create", "name": "x"}) is None


def test_overlap_is_advised_once_then_create_goes_through(make_engine, fake):
    jev(fake, {"styled-pdf-deliverables": 0.7, "pdf": 0.2},
        {"styled-pdf-deliverables": ("duplicate", 0.91, 0.85), "pdf": ("related", 0.2, 0.3)})
    h = harness(make_engine)
    d = h.on_pre_tool_call(tool_name="skill_manage", args=create_args(), session_id="s")
    assert d["action"] == "block"
    assert "styled-pdf-deliverables" in d["message"] and "extending" in d["message"]
    assert "pdf:" not in d["message"]                                    # related, not overlapping
    assert h.on_pre_tool_call(tool_name="skill_manage", args=create_args(), session_id="s") is None
    # a different proposed skill is checked on its own
    assert h.on_pre_tool_call(tool_name="skill_manage", args=create_args(name="other-pdf"), session_id="s")


def test_distinct_skill_is_not_interrupted(make_engine, fake):
    jev(fake, {"pdf": 0.3}, {"pdf": ("related", 0.25, 0.2)})
    assert harness(make_engine).on_pre_tool_call(tool_name="skill_manage", args=create_args(), session_id="s") is None


def test_nothing_shortlisted_means_one_request_only(make_engine, fake):
    jev(fake, {}, {})
    assert harness(make_engine).on_pre_tool_call(tool_name="skill_manage", args=create_args(), session_id="s") is None
    assert len(fake.requests) == 1                                       # no judge call


def test_patch_and_other_tools_are_not_checked(make_engine, fake):
    h = harness(make_engine)
    assert h.on_pre_tool_call(tool_name="skill_manage", args={"action": "patch", "name": "pdf"}, session_id="s") is None
    assert h.on_pre_tool_call(tool_name="read_file", args={"path": "x"}, session_id="s") is None
    assert not fake.requests


def test_shadow_never_blocks_and_logs(make_engine, fake):
    import time

    jev(fake, {"styled-pdf-deliverables": 0.8}, {"styled-pdf-deliverables": ("duplicate", 0.95, 0.9)})
    h = harness(make_engine, mode="shadow")
    assert h.on_pre_tool_call(tool_name="skill_manage", args=create_args(), session_id="s") is None
    for _ in range(50):
        rows = h.engine.store.recent(10, point="skill_overlap")
        if rows:
            break
        time.sleep(0.05)
    assert rows and rows[0]["action"] == "overlap" and rows[0]["applied"] == 0


def test_jev_error_fails_open(make_engine, fake):
    fake.status = 503
    assert harness(make_engine).on_pre_tool_call(tool_name="skill_manage", args=create_args(), session_id="s") is None


def test_bundled_match_wording(make_engine, fake, monkeypatch):
    jev(fake, {"pdf": 0.8}, {"pdf": ("target_contains", 0.88, 0.8)})
    monkeypatch.setattr(so, "skill_sources", lambda roster: {s.name: ("bundled" if s.name == "pdf" else "local")
                                                             for s in roster})
    d = harness(make_engine).on_pre_tool_call(tool_name="skill_manage", args=create_args(), session_id="s")
    assert "bundled skill" in d["message"] and "already covers everything" in d["message"]


def test_audit_groups_pairs_and_suggests_keeper(make_engine, fake, monkeypatch):
    # a<->b duplicate, b contains c: one group of three, keep b. spotify alone.
    roster = [Skill("a", "A", "short"), Skill("b", "B", "much longer body " * 20), Skill("c", "C", "mid " * 5),
              Skill("spotify", "Spotify", "x")]

    def respond(qid, q, state):
        tgt = state["target"]["name"]
        if qid == "which":
            wanted = {"a": {"b": 0.8}, "b": {"a": 0.6, "c": 0.3}, "c": {"b": 0.7}, "spotify": {}}[tgt]
            probs = {o: wanted.get(o, 0.0) for o in q["criteria"]}
            probs[so.NONE_OPTION] = 1 - sum(probs.values())
            best = max(probs, key=probs.get)
            return {"type": "choice", "choice": best, "probabilities": probs, "confidence": 0.8}
        kind, i = qid.split("_")
        other = state["existing"][f"e{i}"]["name"]
        table = {("a", "b"): ("duplicate", 0.9), ("b", "a"): ("duplicate", 0.9),
                 ("b", "c"): ("existing_contains", 0.8), ("c", "b"): ("target_contains", 0.85)}
        rel, same = table.get((tgt, other), ("unrelated", 0.1))
        if kind == "rel":
            return {"type": "choice", "choice": rel, "probabilities": {rel: 0.9}, "confidence": 0.9}
        return {"type": "noul", "noul": same}

    fake.on(respond)
    monkeypatch.setattr(so, "skill_sources", lambda r: {s.name: "local" for s in r})
    eng = make_engine(skill_overlap="shadow")
    rep = skill_audit.audit(eng, roster, progress=lambda s: None)
    assert len(rep["groups"]) == 1
    g = rep["groups"][0]
    assert sorted(g["members"]) == ["a", "b", "c"] and g["keep"] == "b"
    assert "Merge into b" in g["suggestion"]
    assert "Nothing was changed" in skill_audit.render(rep)


def test_audit_prefers_bundled_keeper(make_engine, fake, monkeypatch):
    g = so.Group(members=["my-pdf", "pdf"], pairs=[("my-pdf", "pdf", "duplicate", 0.9)])
    out = so.suggest(g, {"my-pdf": "local", "pdf": "bundled"}, {"my-pdf": 5000, "pdf": 100})
    assert out.keep == "pdf" and "archive it" in out.suggestion and "bundled" in out.suggestion
