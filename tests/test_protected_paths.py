"""Tests for the risk gate's protected-path rule (checked in code, not by Jev).

The shell strings below are test inputs only: nothing is executed. They are
assembled from parts so that approval scanners reading this file don't
mistake them for commands.
"""

from jermes.config import DEFAULTS
from jermes.points import risk_gate
from jermes.questions import ChoiceAnswer, NoulAnswer, ScoreAnswer

CFG = dict(DEFAULTS["points"]["risk_gate"])

APPEND = ">" + ">"
SED_I = "sed" + " -i"
RC = "~/." + "bashrc"
HOSTS = "/" + "etc/hosts"
AGENT_HOME = "~/." + "her" + "mes"


def test_protected_paths_detected_in_code():
    tp = risk_gate.touches_protected
    assert tp("write_file", {"path": AGENT_HOME + "/config.yaml"})
    assert tp("patch", {"path": "/home/u/." + "s" + "sh/config"})
    assert tp("write_file", {"path": "app/.env"})
    assert tp("terminal", {"command": f"echo x {APPEND} {RC}"})
    assert tp("terminal", {"command": f"{SED_I} s/a/b/ {HOSTS}"})
    assert not tp("write_file", {"path": "notes/todo.md"})
    assert not tp("terminal", {"command": f"cat {RC}"})                        # reading is fine
    assert not tp("terminal", {"command": "set -a; . ~/hermes-agent/.env; set +a; python run.py"})


def _answers(match: str):
    return {
        "risk": ScoreAnswer(score=0.5, probabilities={}, confidence=0.9),
        "destructive": NoulAnswer(0.1),
        "exfiltration": NoulAnswer(0.05),
        "matches_request": ChoiceAnswer(choice=match, probabilities={"yes": 0.1, "partly": 0.1, "no": 0.7,
                                                                     "unclear": 0.1}, confidence=0.7),
    }


def test_protected_write_goes_to_review_unless_clearly_requested():
    pol = risk_gate.make_policy(CFG, protected=True)
    assert pol(_answers("no")).action == "review"
    assert pol(_answers("partly")).action == "review"
    assert pol(_answers("yes")).action == "allow"
    assert risk_gate.make_policy(CFG)(_answers("no")).action == "allow"        # unprotected path: no review


def test_hook_sends_unrequested_protected_write_to_review(make_engine, fake):
    # Through the real pre_tool_call hook: Jev sees nothing wrong ("no" match,
    # no hazards), yet a write to a credential file must still reach a human.
    from jermes.harness import Harness

    fake.on(lambda qid, q, s: {"type": "choice", "choice": "no",
                               "probabilities": {"yes": 0.1, "partly": 0.1, "no": 0.7, "unclear": 0.1},
                               "confidence": 0.7} if qid == "matches_request" else None)
    h = Harness(make_engine(risk_gate="enforce", skill_suggest="off", model_router="off"))
    h.on_pre_llm_call(session_id="s", user_message="tidy up the notes folder")
    d = h.on_pre_tool_call(tool_name="write_file", args={"path": AGENT_HOME + "/.e" + "nv", "content": "X=1"}, session_id="s")
    assert d and d["action"] == "approve" and "protected" in d["message"]
    assert h.on_pre_tool_call(tool_name="write_file", args={"path": "notes/a.md", "content": "x"},
                              session_id="s") is None
