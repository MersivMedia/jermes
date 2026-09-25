"""Tasks for the task-matched A/B (``jermes ab``).

Each task writes deterministic fixture files into a scratch directory, gives
the agent one prompt, and checks the result automatically, so both arms are
judged the same way without a human or an LLM grader.

The set is chosen to exercise the places Jermes can save tokens:

* big tool output the agent has to read (``result_filter``)
* requests where one skill is relevant, and chat where none is (``skill_suggest``)
* a small edit with no big output at all (control: Jermes should cost, not save)
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Tuple


@dataclass
class Task:
    name: str
    prompt: str
    setup: Callable[[Path], None]
    check: Callable[[Path], Tuple[bool, str]]
    exercises: str


def _answer(d: Path) -> str:
    p = d / "answer.txt"
    return p.read_text(encoding="utf-8", errors="replace").strip().lower() if p.exists() else ""


# ---------------------------------------------------------------- big log

COMPONENTS = ["auth-service", "billing-worker", "cache-proxy", "search-indexer", "mailer", "image-resizer"]


def _setup_big_log(d: Path) -> None:
    rng = random.Random(7)
    lines = []
    for i in range(1400):
        comp = rng.choice(COMPONENTS)
        lvl = rng.choice(["INFO"] * 12 + ["DEBUG"] * 6 + ["WARN"])
        msg = rng.choice([
            "request completed in {}ms".format(rng.randint(3, 900)),
            "cache hit ratio {:.2f}".format(rng.random()),
            "heartbeat ok",
            "retrying upstream call (attempt {}/3)".format(rng.randint(1, 2)),
            "queue depth {}".format(rng.randint(0, 50)),
        ])
        lines.append(f"2026-09-25T10:{i // 60 % 60:02d}:{i % 60:02d}Z {lvl:<5} [{comp}] {msg}")
    lines.insert(911, "2026-09-25T10:15:11Z ERROR [billing-worker] FATAL: ledger write failed: "
                      "constraint violation on invoices.customer_id (NULL) -- process exiting with code 3")
    (d / "logs").mkdir()
    (d / "logs" / "app.log").write_text("\n".join(lines) + "\n")


def _check_big_log(d: Path) -> Tuple[bool, str]:
    a = _answer(d)
    return ("billing-worker" in a, f"answer.txt={a[:80]!r}")


# ---------------------------------------------------------------- big spec document

def _setup_big_doc(d: Path) -> None:
    rng = random.Random(11)
    words = ("platform tenant workspace quota export audit region latency retention webhook role "
             "invite billing seat sso scim token session storage backup restore").split()
    parts = ["# Product specification v7\n"]
    for s in range(40):
        parts.append(f"\n## Section {s + 1}: {' '.join(rng.sample(words, 3)).title()}\n")
        for _ in range(4):
            parts.append(" ".join(rng.choice(words) for _ in range(60)) + ".\n\n")
    parts.insert(57, "\n## Section 14b: Upload limits\n\nThe maximum single-file upload size on the Team plan "
                     "is 750 MB. Starter is limited to 100 MB and Enterprise to 5 GB.\n\n")
    (d / "spec.md").write_text("".join(parts))


def _check_big_doc(d: Path) -> Tuple[bool, str]:
    a = _answer(d)
    return ("750" in a, f"answer.txt={a[:80]!r}")


# ---------------------------------------------------------------- big JSON dump

def _setup_big_json(d: Path) -> None:
    rng = random.Random(23)
    users = []
    for i in range(500):
        users.append({"id": 1000 + i, "name": f"user{i:03d}", "plan": rng.choice(["starter", "team", "team", "enterprise"]),
                      "region": rng.choice(["us-east", "eu-west", "ap-south"]), "active": rng.random() > 0.2,
                      "seats": rng.randint(1, 40), "notes": " ".join(rng.choice("abcdefgh") * 3 for _ in range(8))})
    users[317].update({"plan": "enterprise", "region": "eu-west", "seats": 400, "active": True})
    (d / "users.json").write_text(json.dumps({"users": users}, indent=1))


def _check_big_json(d: Path) -> Tuple[bool, str]:
    a = _answer(d)
    return ("1317" in a, f"answer.txt={a[:80]!r}")


# ---------------------------------------------------------------- csv -> xlsx (a skill applies)

def _setup_xlsx(d: Path) -> None:
    rows = ["region,q1,q2,q3,q4"] + [f"{r},{i * 10},{i * 11},{i * 12},{i * 13}"
                                    for i, r in enumerate(["north", "south", "east", "west"], 1)]
    (d / "sales.csv").write_text("\n".join(rows) + "\n")


def _check_xlsx(d: Path) -> Tuple[bool, str]:
    p = d / "sales.xlsx"
    if not p.exists():
        return False, "sales.xlsx missing"
    try:
        import zipfile

        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            shared = z.read("xl/sharedStrings.xml").decode() if "xl/sharedStrings.xml" in names else ""
            sheet = "".join(z.read(n).decode() for n in names if n.startswith("xl/worksheets/"))
        ok = "west" in (shared + sheet) and "52" in sheet
        return ok, "valid xlsx" if ok else "xlsx missing expected cells"
    except Exception as exc:
        return False, f"not a valid xlsx: {exc}"


# ---------------------------------------------------------------- chat (no skill applies)

def _setup_none(d: Path) -> None:
    pass


def _check_monad(d: Path) -> Tuple[bool, str]:
    a = _answer(d)
    ok = "monad" in a and len(a) < 1200 and len(a) > 40
    return ok, f"{len(a)} chars"


# ---------------------------------------------------------------- small code fix (control)

def _setup_fix(d: Path) -> None:
    (d / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs) + 1\n")
    (d / "test_calc.py").write_text("from calc import average\n\n\ndef test_average():\n"
                                    "    assert average([2, 4, 6]) == 4\n")


def _check_fix(d: Path) -> Tuple[bool, str]:
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"calc_{abs(hash(str(d)))}", d / "calc.py")
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # the scratch fixture written by _setup_fix
        ok = mod.average([2, 4, 6]) == 4 and mod.average([1, 2]) == 1.5
    except Exception as exc:
        return False, f"error: {exc}"
    return ok, "average() fixed" if ok else "still wrong"


# ---------------------------------------------------------------- long transcript (must be read in full)

_TOPICS = [
    ("hiring", ["We have two open backend roles.", "The recruiter wants the job post live by Friday.",
                "Interview loop stays at four rounds."]),
    ("infra", ["Database CPU spiked twice last week.", "We could add a read replica.",
               "The on-call rotation needs a fifth person."]),
    ("design", ["The new onboarding flow tested well.", "Mobile still has the cramped settings page.",
                "Dark mode ships behind a flag."]),
    ("support", ["Ticket volume is up twelve percent.", "Most tickets are password resets.",
                 "We should add a self-serve reset link."]),
    ("finance", ["Cloud spend came in under budget.", "The annual plan discount is still twenty percent.",
                 "Procurement wants the vendor list by month end."]),
]
_PEOPLE = ["Priya", "Marcus", "Dana", "Tomas", "Aiko", "Lena", "Omar"]


def _setup_transcript(d: Path) -> None:
    """A long all-hands transcript. The launch date is proposed early and changed
    twice; the owner of the pricing page is reassigned near the end. Searching for
    one keyword finds the stale answers first."""
    rng = random.Random(41)
    lines = ["# Weekly all-hands transcript", ""]

    def filler(n):
        for _ in range(n):
            topic, facts = rng.choice(_TOPICS)
            who = rng.choice(_PEOPLE)
            lines.append(f"{who} ({topic}): {rng.choice(facts)} "
                         + " ".join(rng.choice(facts).lower().rstrip('.') for _ in range(2)) + ".")

    filler(120)
    lines.append("Marcus (launch): Proposal: we launch v3 on October 6. Priya will own the pricing page.")
    filler(140)
    lines.append("Dana (launch): QA found blockers, so October 6 is off the table. Let's say October 20 for now.")
    filler(150)
    lines.append("Priya (launch): Final call from leadership: v3 launches November 3. That's locked.")
    filler(90)
    lines.append("Omar (launch): Priya is moving to the billing project, so Tomas takes over the pricing page.")
    filler(60)
    (d / "transcript.md").write_text("\n".join(lines) + "\n")


def _check_transcript(d: Path) -> Tuple[bool, str]:
    a = _answer(d)
    ok = "november 3" in a and "tomas" in a and "october" not in a and "priya" not in a
    return ok, f"answer.txt={a[:80]!r}"


def _setup_release_notes(d: Path) -> None:
    """Long release notes; the one breaking change is described in prose, with no
    keyword like 'breaking' to search for."""
    rng = random.Random(53)
    areas = ["search", "exports", "billing", "auth", "webhooks", "admin", "reports", "mobile"]
    verbs = ["improved", "fixed a rare crash in", "sped up", "polished the UI of", "added logging to",
             "reduced memory use in", "clarified error messages in"]
    parts = ["# Release notes 7.0 to 7.40\n"]
    for v in range(40):
        parts.append(f"\n## 7.{v}\n")
        for _ in range(9):
            parts.append(f"- {rng.choice(verbs).capitalize()} {rng.choice(areas)}; "
                         f"{' '.join(rng.choice(areas) for _ in range(12))} internal cleanup.\n")
        if v == 27:
            parts.append("- The webhooks endpoint now signs payloads with SHA-256 only; receivers that "
                         "still verify SHA-1 signatures will reject every delivery after upgrading.\n")
    (d / "RELEASE_NOTES.md").write_text("".join(parts))


def _check_release_notes(d: Path) -> Tuple[bool, str]:
    a = _answer(d)
    return ("7.27" in a and "webhook" in a), f"answer.txt={a[:80]!r}"


TASKS: List[Task] = [
    Task("big_log", "logs/app.log is our service log from this morning. Find the error that crashed the service "
         "and write only the name of the failing component to answer.txt.", _setup_big_log, _check_big_log,
         "result_filter (large log)"),
    Task("big_doc", "Using spec.md, what is the maximum single-file upload size on the Team plan? Write just the "
         "size to answer.txt.", _setup_big_doc, _check_big_doc, "result_filter (large document)"),
    Task("big_json", "users.json is an export of our customer accounts. Which user id has the most seats? "
         "Write just the id to answer.txt.", _setup_big_json, _check_big_json, "result_filter (large JSON)"),
    Task("csv_to_xlsx", "Convert sales.csv into an Excel workbook named sales.xlsx in this folder.",
         _setup_xlsx, _check_xlsx, "skill_suggest (a skill applies)"),
    Task("explain_monad", "In three plain sentences, explain what a monad is in programming. Write the answer "
         "to answer.txt and nothing else.", _setup_none, _check_monad, "skill_suggest (no skill applies)"),
    Task("fix_average", "The test in test_calc.py fails. Fix calc.py so it passes. Don't change the test.",
         _setup_fix, _check_fix, "control (little to save)"),
    Task("transcript", "transcript.md is this week's all-hands. Decisions changed during the meeting, so read the "
         "whole transcript rather than searching it. Then write the final agreed launch date for v3 and the "
         "person who ends up owning the pricing page to answer.txt, one line, nothing else.",
         _setup_transcript, _check_transcript, "result_filter (full read of a long file, follow-up turns)"),
    Task("release_notes", "Read RELEASE_NOTES.md in full. Which release introduced a change that will break "
         "existing integrations after upgrading, and what breaks? Write the version and a one-line reason "
         "to answer.txt.", _setup_release_notes, _check_release_notes,
         "result_filter (full read, answer has no keyword to grep)"),
]

BY_NAME = {t.name: t for t in TASKS}
