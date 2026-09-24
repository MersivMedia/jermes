"""D6: loop and completion notes (PRD section 5.6).

Keeps a short rolling window of tool calls per session. On each new result it
asks whether the latest call repeats one that already failed, and every few
calls whether the results so far already satisfy the request. When a signal
fires (advise/enforce), a one-line harness note is appended to the tool result,
which is new tail content and therefore prompt-cache safe. Advisory only: the
reasoning model still decides what to do.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Mapping

from ..engine import Verdict
from ..questions import Noul

SPEC_VERSION = "loop_guard.1"


class History:
    def __init__(self, window: int = 8) -> None:
        self.window = window
        self._lock = threading.Lock()
        self._calls: Dict[str, Deque[Dict[str, str]]] = defaultdict(lambda: deque(maxlen=self.window))
        self._requests: Dict[str, str] = {}
        self._counts: Dict[str, int] = defaultdict(int)

    def set_request(self, session_id: str, user_message: str) -> None:
        with self._lock:
            self._requests[session_id] = user_message or ""
            self._calls[session_id].clear()
            self._counts[session_id] = 0

    def request(self, session_id: str) -> str:
        return self._requests.get(session_id, "")

    def add(self, session_id: str, tool_name: str, args: Mapping[str, Any], result: str, status: str) -> int:
        entry = {
            "tool": tool_name,
            "args": json.dumps(args, sort_keys=True, default=str)[:600],
            "status": status or "",
            "result_excerpt": (result or "")[:600],
        }
        with self._lock:
            self._calls[session_id].append(entry)
            self._counts[session_id] += 1
            return self._counts[session_id]

    def snapshot(self, session_id: str) -> List[Dict[str, str]]:
        with self._lock:
            return list(self._calls[session_id])


def build(request: str, calls: List[Dict[str, str]], ask_completion: bool):
    state = {
        "user_request": request[:3000],
        "earlier_calls": calls[:-1],
        "latest_call": calls[-1] if calls else {},
    }
    qs: Dict[str, Any] = {
        "repeat_failure": Noul(
            instructions=(
                "Is `latest_call` essentially the same action as one in `earlier_calls` that already "
                "failed or returned an error, with no meaningful change in approach?"
            )
        )
    }
    if ask_completion:
        qs["done"] = Noul(
            instructions=(
                "Taken together, do the results in `earlier_calls` and `latest_call` already contain "
                "everything needed to fully answer `user_request`?"
            )
        )
    return state, qs


def make_policy(cfg: Mapping[str, Any]):
    done_t = float(cfg.get("completion_threshold", 0.85))

    def policy(a: Dict[str, Any]) -> Verdict:
        rep = a["repeat_failure"].noul
        done = a["done"].noul if "done" in a else 0.0
        detail = {"repeat_failure": round(rep, 3), "done": round(done, 3)}
        if rep >= 0.8:
            return Verdict("note_loop", detail)
        if done >= done_t:
            return Verdict("note_done", detail)
        return Verdict("none", detail)

    return policy


NOTES = {
    "note_loop": "[jermes: this repeats an earlier call that already failed. Change approach or ask the user.]",
    "note_done": "[jermes: the results so far look sufficient to answer the request. Consider responding now.]",
}


def append_note(result: str, note: str) -> str:
    try:
        data = json.loads(result)
        if isinstance(data, dict):
            data = dict(data)
            data["_harness_note"] = note
            return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        pass
    return f"{result}\n\n{note}"
