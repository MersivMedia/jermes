import csv

from jermes import labels
from jermes.labels import Counts, Label, _parse_answer
from jermes.replay import Turn

KNOWN = ["research-design-documents", "grounded-citations", "google-workspace", "comfyui"]


def test_counts_metrics_on_sets():
    c = Counts()
    c.add(["research-design-documents", "grounded-citations"], ["grounded-citations", "research-design-documents"])
    c.add(["comfyui"], ["google-workspace"])        # wrong skill
    c.add([], ["google-workspace"])                 # miss
    c.add(["comfyui"], [])                          # false alarm
    c.add([], [])                                   # correct "none"
    r = c.report()
    assert r["turns"] == 5 and r["needs_skill_turns"] == 3 and r["none_turns"] == 2
    assert r["decision_accuracy_pct"] == 60.0       # turns 1, 2, 5 got skill-vs-none right
    assert r["primary_hit_pct"] == 33.3             # only turn 1
    assert r["recall_pct"] == 50.0                  # 2 of 4 labelled skills listed
    assert r["precision_pct"] == 50.0               # 2 of 4 listed skills correct
    assert r["miss_pct"] == 33.3 and r["false_alarm_pct"] == 50.0


def test_names_compare_on_leaf():
    c = Counts()
    c.add(["creative/notebooklm-brand-edit"], ["notebooklm-brand-edit"])
    assert c.report()["primary_hit_pct"] == 100.0


def test_parse_answer_shortcuts_and_validation():
    jev, agent = ["google-workspace"], ["grounded-citations"]
    assert _parse_answer("", jev, agent, KNOWN) == ("label", jev)
    assert _parse_answer("a", jev, agent, KNOWN) == ("label", agent)
    assert _parse_answer("n", jev, agent, KNOWN) == ("label", [])
    assert _parse_answer("s", jev, agent, KNOWN)[0] == "skip"
    assert _parse_answer("q", jev, agent, KNOWN)[0] == "quit"
    kind, val = _parse_answer("Research-Design-Documents, grounded-citations", jev, agent, KNOWN)
    assert kind == "label" and val == ["research-design-documents", "grounded-citations"]
    kind, msg = _parse_answer("grounded", jev, agent, KNOWN)
    assert kind == "error" and "grounded-citations" in msg  # suggests the real name


def test_labels_file_latest_wins_and_skip_removes(tmp_path):
    p = tmp_path / "labels.jsonl"
    labels.append_label(Label("s#1", "req", ["comfyui"]), p)
    labels.append_label(Label("s#1", "req", []), p)          # relabelled as none
    labels.append_label(Label("s#2", "req2", ["comfyui"]), p)
    labels.append_label(Label("s#2", "req2"), p, skip=True)  # dropped
    got = labels.load_labels(p)
    assert list(got) == ["s#1"] and got["s#1"].none


def test_label_file_redacts_secrets(tmp_path):
    p = tmp_path / "labels.jsonl"
    key = "vck_" + "Q1w2E3r4T5y6U7i8O9p0AsDfGhJkLzXc"
    labels.append_label(Label("s#1", f"my key {key}", []), p)
    assert key not in p.read_text()


def _turns():
    return [
        Turn("s", 1, "write a research doc with citations", ["grounded-citations"], [],
             [{"role": "user", "content": "earlier"}]),
        Turn("s", 2, "thanks!", [], [], []),
    ]


def test_interactive_session(tmp_path):
    p = tmp_path / "labels.jsonl"
    answers = iter(["research-design-documents, grounded-citations", "n"])
    rank = lambda t: ["research-design-documents"] if t.message_id == 1 else []
    n = labels.interactive(_turns(), rank, KNOWN, path=p, ask=lambda _: next(answers), out=lambda *_: None)
    got = labels.load_labels(p)
    assert n == 2 and got["s#1"].skills == ["research-design-documents", "grounded-citations"] and got["s#2"].none
    # Resuming offers nothing already labelled.
    assert labels.interactive(_turns(), rank, KNOWN, path=p, ask=lambda _: "q", out=lambda *_: None) == 0


def test_interactive_reprompts_on_typo_and_quits(tmp_path):
    p = tmp_path / "labels.jsonl"
    answers = iter(["grounded", "grounded-citations", "q"])
    labels.interactive(_turns(), lambda t: [], KNOWN, path=p, ask=lambda _: next(answers), out=lambda *_: None)
    assert labels.load_labels(p)["s#1"].skills == ["grounded-citations"]


def test_sheet_round_trip(tmp_path):
    p = tmp_path / "labels.jsonl"
    sheet = tmp_path / "sheet.csv"
    assert labels.export_sheet(_turns(), lambda t: ["comfyui"], sheet, path=p) == 2
    rows = list(csv.DictReader(sheet.open()))
    assert rows[0]["jev_lists"] == "comfyui" and rows[0]["earlier_conversation"].startswith("user: earlier")
    rows[0]["correct_skills"] = "grounded-citations, research-design-documents"
    rows[1]["correct_skills"] = "none"
    with sheet.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=labels.SHEET_COLS)
        w.writeheader()
        w.writerows(rows)
    rep = labels.import_sheet(sheet, KNOWN, path=p)
    assert rep == {"added": 2, "blank": 0, "problems": []}
    got = labels.load_labels(p)
    assert got["s#1"].skills == ["grounded-citations", "research-design-documents"] and got["s#2"].none


def test_sheet_import_reports_typos(tmp_path):
    sheet = tmp_path / "sheet.csv"
    sheet.write_text("key,request,earlier_conversation,agent_loaded,jev_lists,correct_skills\ns#1,r,,,,comfy-ui-typo\n")
    rep = labels.import_sheet(sheet, KNOWN, path=tmp_path / "l.jsonl")
    assert rep["added"] == 0 and rep["problems"]


def test_score_compares_jev_and_agent():
    labs = [Label("s#1", "r", ["grounded-citations"], agent_loaded=["google-workspace"]),
            Label("s#2", "r", [], agent_loaded=[])]
    rep = labels.score(labs, lambda lab: ["grounded-citations"] if lab.key == "s#1" else [])
    assert rep["jev"]["decision_accuracy_pct"] == 100.0 and rep["jev"]["primary_hit_pct"] == 100.0
    assert rep["agent"]["primary_hit_pct"] == 0.0
    assert all(r["jev_ok"] for r in rep["rows"])
    unavailable = labels.score(labs, lambda lab: None)
    assert unavailable["errors"] == 2 and unavailable["jev"]["turns"] == 0


def test_jev_ok_is_a_real_bool_that_survives_json():
    import json

    labs = [Label("s#1", "r", ["runpod-pods"]), Label("s#2", "r", []), Label("s#3", "r", ["comfyui"])]
    preds = {"s#1": [], "s#2": [], "s#3": ["comfyui", "x"]}
    rep = json.loads(json.dumps(labels.score(labs, lambda lab: preds[lab.key]), default=str))
    assert [r["jev_ok"] for r in rep["rows"]] == [False, True, True]
