"""The skill roster is read on every call, so skills created mid-session are seen.

Runs against a real Hermes checkout (Hermes' own skill discovery) in a temp
HERMES_HOME. Skipped when Hermes is not importable.
"""

import os
import sys
import time
from pathlib import Path

import pytest

HERMES_DIR = os.environ.get("HERMES_AGENT_DIR", str(Path.home() / "hermes-agent"))
pytestmark = pytest.mark.skipif(not (Path(HERMES_DIR) / "tools" / "skills_tool.py").exists(),
                                reason="Hermes Agent checkout not found")


def _write_skill(root: Path, name: str, desc: str, body: str = "Steps.") -> Path:
    d = root / "skills" / "testing" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n\n{body}\n")
    return d


def test_new_skill_seen_on_next_call(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    if HERMES_DIR not in sys.path:
        monkeypatch.syspath_prepend(HERMES_DIR)
    _write_skill(home, "alpha-skill", "Does alpha things.")

    from jermes.harness import Harness
    from jermes.points import skill_suggest

    h = Harness.__new__(Harness)  # roster() needs no engine
    h._roster = None
    first = {s.name for s in h.roster()}
    assert "alpha-skill" in first and "beta-skill" not in first

    # Created mid-session. Bump mtime past the cache signature's resolution.
    time.sleep(0.01)
    _write_skill(home, "beta-skill", "Does beta things.", body="Beta procedure body.")
    os.utime(home / "skills" / "testing", None)
    os.utime(home / "skills", None)

    second = {s.name: s for s in h.roster()}
    assert "beta-skill" in second
    assert "Beta procedure body." in skill_suggest.skill_body(second["beta-skill"])


def test_disabled_skills_are_not_offered(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    if HERMES_DIR not in sys.path:
        monkeypatch.syspath_prepend(HERMES_DIR)
    _write_skill(home, "keep-me", "Kept.")
    _write_skill(home, "turned-off", "Disabled by the user.")
    (home / "config.yaml").write_text("skills:\n  disabled: [turned-off]\n")
    try:
        from agent import skill_utils
        skill_utils._raw_config_cache_clear()
    except Exception:
        pass

    from jermes.points import skill_suggest

    names = {s.name for s in skill_suggest.current_roster()}
    assert "keep-me" in names and "turned-off" not in names
