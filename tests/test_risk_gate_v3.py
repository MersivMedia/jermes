"""risk_gate.3 policy rules: weakens-safety and against-the-request blocks, routine allowance."""

from jermes.config import DEFAULTS
from jermes.points import risk_gate
from jermes.questions import ChoiceAnswer, NoulAnswer, ScoreAnswer

CFG = dict(DEFAULTS["points"]["risk_gate"])


def ans(*, risk=0.5, conf=0.9, des=0.05, exf=0.05, weak=0.05, against=0.05, irrev=0.05, yes=0.8, no=0.1, inj=None):
    a = {
        "risk": ScoreAnswer(score=risk, probabilities={}, confidence=conf),
        "destructive": NoulAnswer(des), "exfiltration": NoulAnswer(exf),
        "weakens_safety": NoulAnswer(weak), "overrides": NoulAnswer(against), "irreversible": NoulAnswer(irrev),
        "matches_request": ChoiceAnswer(choice="yes" if yes >= no else "no",
                                        probabilities={"yes": yes, "no": no, "partly": 1 - yes - no},
                                        confidence=max(yes, no)),
    }
    if inj is not None:
        a["injected"] = NoulAnswer(inj)
    return a


def test_questions_ask_the_new_hazards():
    qs = risk_gate.questions(False)
    assert {"weakens_safety", "overrides", "irreversible"} <= set(qs)
    assert "injected" in risk_gate.questions(True)


def test_unrequested_safety_weakening_is_blocked():
    v = risk_gate.make_policy(CFG)(ans(weak=0.9, yes=0.1, no=0.8))
    assert v.action == "block" and "safety" in v.detail["reason"]


def test_requested_safety_change_is_not_blocked():
    v = risk_gate.make_policy(CFG)(ans(weak=0.9, yes=0.95, no=0.0))
    assert v.action == "review"            # still a human, but not refused


def test_hazard_against_the_request_is_blocked_even_if_partly_matched():
    # "fix the typo, don't change anything else" -> the call also turns approvals off
    v = risk_gate.make_policy(CFG)(ans(weak=0.65, against=0.9, yes=0.3, no=0.1))
    assert v.action == "block" and "goes against" in v.detail["reason"]


def test_against_the_request_without_a_hazard_is_review_not_block():
    v = risk_gate.make_policy(CFG)(ans(against=0.9))
    assert v.action == "review"


def test_routine_requested_work_is_not_reviewed_on_risk_level_alone():
    # e.g. "trigger the staging deploy": external side effect, clearly asked, easy to undo
    pol = risk_gate.make_policy(CFG)
    assert pol(ans(risk=3.0, irrev=0.1)).action == "allow"
    assert pol(ans(risk=3.0, irrev=0.6)).action == "review"          # hard to undo: still a human
    assert pol(ans(risk=3.0, yes=0.4, no=0.1)).action == "review"    # not clearly asked


def test_secret_sink_and_protected_rules_unchanged():
    assert risk_gate.make_policy(CFG, secret_sink=True)(ans()).action == "review"
    assert risk_gate.make_policy(CFG, protected=True)(ans(yes=0.3, no=0.6)).action == "review"


def test_old_answer_sets_without_new_questions_still_work():
    a = ans()
    for k in ("weakens_safety", "overrides", "irreversible"):
        a.pop(k)
    assert risk_gate.make_policy(CFG)(a).action == "allow"
    assert risk_gate.make_policy(CFG)({**a, "risk": ScoreAnswer(score=3.0, probabilities={}, confidence=0.9)}).action == "review"
