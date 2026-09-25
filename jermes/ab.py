"""Task-matched A/B: the same tasks with Jermes off and on, measured by Hermes.

For every (task, arm, repeat) the runner:

1. creates a fresh HERMES_HOME that shares only your model credentials and
   skills (no sessions, no memory, no other plugins), so runs cannot see each
   other's history;
2. writes the task's fixture files into a scratch working directory;
3. runs ``hermes chat -Q -q <prompt>`` there;
4. reads token usage and cost from that run's own session row in Hermes'
   ``state.db`` (the same numbers Hermes bills on), and checks the result.

Arms:

* ``off``: Jermes not installed at all.
* ``on``: Jermes installed and enabled with the chosen decision points in
  ``enforce``/``advise`` mode (the only modes that change what the model sees).

Each arm's cost is reported with Jev's own cost added to the "on" side.
Tasks alternate arm order to avoid systematic order effects.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ab_tasks import BY_NAME, TASKS, Task
from .savings import JEV_INPUT_PER_M, Prices

REPO = Path(__file__).resolve().parents[1]

ON_CONFIG = """\
backend:
  deadline_s: 6
points:
  skill_suggest: {{mode: {skill}}}
  result_filter: {{mode: {filt}}}
  risk_gate: {{mode: shadow}}
  loop_guard: {{mode: shadow}}
  model_router: {{mode: off}}
"""


@dataclass
class Run:
    task: str
    arm: str
    repeat: int
    ok: bool = False
    check: str = ""
    seconds: float = 0.0
    exit_code: int = 0
    model: str = ""
    api_calls: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    jev_tokens: int = 0
    jev_usd: float = 0.0
    applied: Dict[str, int] = field(default_factory=dict)
    error: str = ""

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_usd(self) -> float:
        return self.usd + self.jev_usd


def _hermes_bin() -> List[str]:
    exe = shutil.which("hermes")
    if exe:
        return [exe]
    hermes_dir = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / "hermes-agent"))
    py = hermes_dir / ".venv" / "bin" / "python"
    return [str(py if py.exists() else sys.executable), "-m", "hermes_cli.main"]


# Variables a parent Hermes process exports for its own session. A child run
# that inherits them starts in the parent's working directory, attaches to the
# parent's session, or thinks it is inside the gateway.
_PARENT_PREFIXES = ("HERMES_", "_HERMES", "TERMINAL_", "JERMES_", "BROWSER_", "SUDO_")


def clean_env(home: Path, work: Path, arm: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_PARENT_PREFIXES)}
    env["HERMES_HOME"] = str(home)
    env["TERMINAL_CWD"] = str(work)
    env["PWD"] = str(work)
    if arm == "off":  # the off arm must not reach Jev by any route
        for k in ("AI_GATEWAY_API_KEY", "TYPESAFE_API_KEY"):
            env.pop(k, None)
    return env


def _make_home(src: Path, arm: str, points: Dict[str, str]) -> Path:
    home = Path(tempfile.mkdtemp(prefix=f"jermes-ab-{arm}-"))
    for name in (".env", "auth.json"):
        if (src / name).exists():
            shutil.copy2(src / name, home / name)
            os.chmod(home / name, 0o600)
    # Same model/provider settings as the user's install, nothing else.
    import yaml

    cfg: Dict[str, Any] = {}
    try:
        user = yaml.safe_load((src / "config.yaml").read_text()) or {}
        for key in ("model", "providers", "auxiliary"):
            if key in user:
                cfg[key] = user[key]
    except Exception:
        pass
    cfg.setdefault("agent", {})["max_turns"] = 40
    cfg["memory"] = {"memory_enabled": False, "user_profile_enabled": False}
    cfg["approvals"] = {"mode": "off"}
    if (src / "skills").exists():
        os.symlink(src / "skills", home / "skills", target_is_directory=True)
    if arm == "on":
        (home / "plugins").mkdir()
        os.symlink(REPO, home / "plugins" / "jermes", target_is_directory=True)
        cfg["plugins"] = {"enabled": ["jermes"]}
        (home / "jermes").mkdir()
        (home / "jermes" / "config.yaml").write_text(ON_CONFIG.format(**points))
    (home / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    return home


def _session_usage(home: Path) -> Optional[Dict[str, Any]]:
    db = home / "state.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        r = conn.execute(
            "SELECT model, coalesce(sum(api_call_count),0), coalesce(sum(input_tokens),0), "
            "coalesce(sum(cache_read_tokens),0), coalesce(sum(cache_write_tokens),0), coalesce(sum(output_tokens),0) "
            "FROM sessions"
        ).fetchone()
    finally:
        conn.close()
    return dict(zip(["model", "api_calls", "input_tokens", "cache_read_tokens", "cache_write_tokens",
                     "output_tokens"], r))


def _jev_usage(home: Path) -> Dict[str, Any]:
    db = home / "jermes" / "decisions.sqlite"
    out = {"jev_tokens": 0, "applied": {}}
    if not db.exists():
        return out
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out["jev_tokens"] = conn.execute(
            "SELECT coalesce(sum(input_tokens),0) FROM decisions WHERE cached=0 AND error IS NULL").fetchone()[0]
        for point, n in conn.execute("SELECT point, count(*) FROM decisions WHERE applied=1 GROUP BY point"):
            out["applied"][point] = n
    finally:
        conn.close()
    return out


def run_one(task: Task, arm: str, repeat: int, *, src_home: Path, points: Dict[str, str], timeout: int,
            keep: bool = False) -> Run:
    home = _make_home(src_home, arm, points)
    work = Path(tempfile.mkdtemp(prefix=f"jermes-ab-work-{task.name}-"))
    task.setup(work)
    run = Run(task.name, arm, repeat)
    env = clean_env(home, work, arm)
    t0 = time.monotonic()
    try:
        p = subprocess.run(_hermes_bin() + ["chat", "-Q", "--yolo", "-q", task.prompt], cwd=work, env=env,
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        run.exit_code = p.returncode
        if p.returncode != 0:
            run.error = (p.stderr or p.stdout)[-400:]
    except subprocess.TimeoutExpired:
        run.error = f"timeout after {timeout}s"
        run.exit_code = -1
    run.seconds = round(time.monotonic() - t0, 1)
    run.ok, run.check = task.check(work)
    usage = _session_usage(home)
    if usage:
        for k, v in usage.items():
            setattr(run, k, v or (0 if k != "model" else ""))
    jev = _jev_usage(home)
    run.jev_tokens, run.applied = int(jev["jev_tokens"]), jev["applied"]
    run.jev_usd = run.jev_tokens * JEV_INPUT_PER_M / 1e6
    p_ = Prices.for_model(run.model) if run.model else None
    if p_:
        run.usd = (run.input_tokens * p_.input + run.cache_read_tokens * p_.cache_read
                   + run.cache_write_tokens * p_.cache_write + run.output_tokens * p_.output) / 1e6
    if not keep:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    return run


def preflight(src_home: Path) -> List[str]:
    """Problems that would make the comparison meaningless, found before any spend."""
    from .config import detect_backend
    from .client import BACKENDS

    problems = []
    name = detect_backend()
    key_env = BACKENDS[name]["api_key_env"]
    if not os.environ.get(key_env):
        problems.append(f"no Jev key in this environment ({key_env}); the 'on' arm would behave like 'off'")
    if not ((src_home / ".env").exists() or (src_home / "auth.json").exists()):
        problems.append(f"no model credentials found in {src_home}")
    return problems


def run_ab(task_names: List[str], repeats: int = 1, *, points: Optional[Dict[str, str]] = None,
           src_home: Optional[Path] = None, timeout: int = 900, out_path: Optional[Path] = None,
           progress=print) -> List[Run]:
    points = points or {"skill": "advise", "filt": "enforce"}
    src_home = src_home or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    problems = preflight(src_home)
    if problems:
        raise RuntimeError("; ".join(problems))
    runs: List[Run] = []
    tasks = [BY_NAME[n] for n in task_names]
    for rep in range(repeats):
        for i, task in enumerate(tasks):
            order = ["off", "on"] if (i + rep) % 2 == 0 else ["on", "off"]
            for arm in order:
                r = run_one(task, arm, rep, src_home=src_home, points=points, timeout=timeout)
                runs.append(r)
                if progress:
                    progress(f"  {task.name:<14} {arm:<3} rep{rep}  {'PASS' if r.ok else 'FAIL'}  "
                             f"{r.api_calls:>3} calls  {r.prompt_tokens / 1e3:>7.1f}k prompt  "
                             f"{r.output_tokens / 1e3:>5.1f}k out  ${r.total_usd:.4f}  {r.seconds:>5.0f}s"
                             + (f"  applied={r.applied}" if r.applied else "") + (f"  ERR {r.error[:80]}" if r.error else ""))
                if out_path:
                    out_path.write_text(json.dumps([asdict(x) for x in runs], indent=1))
    return runs


def summarize(runs: List[Run]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"tasks": {}, "overall": {}}
    for arm in ("off", "on"):
        rs = [r for r in runs if r.arm == arm]
        out["overall"][arm] = {
            "runs": len(rs), "passed": sum(r.ok for r in rs),
            "prompt_tokens": sum(r.prompt_tokens for r in rs), "output_tokens": sum(r.output_tokens for r in rs),
            "usd": round(sum(r.total_usd for r in rs), 4), "jev_usd": round(sum(r.jev_usd for r in rs), 5),
        }
    for name in dict.fromkeys(r.task for r in runs):
        row = {}
        for arm in ("off", "on"):
            rs = [r for r in runs if r.task == name and r.arm == arm]
            if rs:
                row[arm] = {"pass": f"{sum(r.ok for r in rs)}/{len(rs)}",
                            "prompt_tokens": statistics.median(r.prompt_tokens for r in rs),
                            "usd": round(statistics.median(r.total_usd for r in rs), 4),
                            "api_calls": statistics.median(r.api_calls for r in rs)}
        out["tasks"][name] = row
    o, n = out["overall"]["off"], out["overall"]["on"]
    if o["prompt_tokens"]:
        out["overall"]["prompt_token_change_pct"] = round(100 * (n["prompt_tokens"] - o["prompt_tokens"]) / o["prompt_tokens"], 1)
    if o["usd"]:
        out["overall"]["cost_change_pct"] = round(100 * (n["usd"] - o["usd"]) / o["usd"], 1)
    return out


def print_summary(s: Dict[str, Any], out=print) -> None:
    out("")
    out(f"  {'task':<14} {'off: pass  prompt tok   $':>30}   {'on: pass  prompt tok   $':>30}")
    for name, row in s["tasks"].items():
        def f(a):
            x = row.get(a)
            return f"{x['pass']:>5} {x['prompt_tokens'] / 1e3:>9.1f}k ${x['usd']:.4f}" if x else "-"
        out(f"  {name:<14} {f('off'):>30}   {f('on'):>30}")
    o, n = s["overall"]["off"], s["overall"]["on"]
    out("")
    out(f"  total off: {o['passed']}/{o['runs']} passed, {o['prompt_tokens'] / 1e3:.0f}k prompt tokens, ${o['usd']:.3f}")
    out(f"  total on:  {n['passed']}/{n['runs']} passed, {n['prompt_tokens'] / 1e3:.0f}k prompt tokens, ${n['usd']:.3f}"
        f" (incl. Jev ${n['jev_usd']:.4f})")
    if "prompt_token_change_pct" in s["overall"]:
        out(f"  change: prompt tokens {s['overall']['prompt_token_change_pct']:+.1f}%, "
            f"cost {s['overall'].get('cost_change_pct', 0):+.1f}%")
