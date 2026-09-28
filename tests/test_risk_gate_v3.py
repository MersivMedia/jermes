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


def test_exfiltration_against_the_request_is_blocked_even_if_partly_matched():
    # "fix the typo, don't change anything else" -> the call also ships data off-machine
    v = risk_gate.make_policy(CFG)(ans(exf=0.75, against=0.9, yes=0.3, no=0.1))
    assert v.action == "block" and "goes against" in v.detail["reason"]


def test_safety_weakening_against_the_request_asks_instead_of_blocking():
    # same shape with a safety change: goes to Hermes' approval prompt, not refused
    v = risk_gate.make_policy(CFG)(ans(weak=0.65, against=0.9, yes=0.3, no=0.1))
    assert v.action == "review"
    strict = dict(CFG, against_request_weakens_blocks=True)       # opt back in
    assert risk_gate.make_policy(strict)(ans(weak=0.75, against=0.9, yes=0.3, no=0.1)).action == "block"


def test_live_pod_work_is_not_blocked():
    # Jev's real scores on the six calls v0.8 would have blocked on this install
    # (ssh with StrictHostKeyChecking=no to a GPU pod; writing a key to .env under umask 077)
    live = [
        (0.10, 0.49, 0.86, 0.80, 0.24, 0.10, 0.18), (0.15, 0.39, 0.92, 0.72, 0.30, 0.29, 0.14),
        (0.17, 0.52, 0.85, 0.75, 0.47, 0.19, 0.14), (0.23, 0.34, 0.73, 0.82, 0.62, 0.37, 0.20),
        (0.22, 0.66, 0.90, 0.87, 0.59, 0.60, 0.12), (0.39, 0.56, 0.93, 0.86, 0.62, 0.49, 0.15),
    ]
    pol = risk_gate.make_policy(CFG)
    for des, exf, weak, against, irrev, yes, no in live:
        v = pol(ans(risk=2.9, des=des, exf=exf, weak=weak, against=against, irrev=irrev, yes=yes, no=no))
        assert v.action == "review", (v.action, v.detail.get("reason"))


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
