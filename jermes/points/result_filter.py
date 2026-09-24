"""D5: tool-result relevance filter (PRD section 5.5).

Bulky text results (web pages, big files, long terminal output) are re-sent to
the reasoning model on every later call in the session. This point splits a
result into chunks, asks one relevance Noul per chunk in a single batched Jev
request, and keeps only relevant chunks in their original order.

Fail-open rules (all enforced in code):
  * never filter errors, mutating tools, or anything under ``min_chars``
  * if Jev fails, or keeps (almost) everything, or keeps nothing, pass through
  * always tell the model that content was elided and where the full text is
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..engine import Verdict
from ..questions import Noul

SPEC_VERSION = "result_filter.1"
ELIDED = "[… {n} less relevant section(s) omitted by jermes …]"


def extract_text(result: str) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """Return (text, json_wrapper, key) for a tool result.

    Hermes tool results are usually JSON strings. We filter the largest string
    field (``content``, ``output``, ``result``, ``text``...) and put it back, so
    the result stays valid JSON for the model.
    """
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return result, None, None
    if isinstance(data, dict):
        if data.get("error") or data.get("success") is False:
            return None, None, None
        best_key, best_len = None, 0
        for k, v in data.items():
            if isinstance(v, str) and len(v) > best_len:
                best_key, best_len = k, len(v)
        if best_key:
            return data[best_key], data, best_key
    return None, None, None


def chunk(text: str, max_chunks: int) -> List[str]:
    """Split on blank lines; merge small pieces so we stay under max_chunks."""
    parts = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) <= 1:
        lines = text.splitlines()
        size = max(1, len(lines) // max_chunks + 1)
        parts = ["\n".join(lines[i : i + size]) for i in range(0, len(lines), size)]
    if len(parts) > max_chunks:
        per = len(parts) // max_chunks + 1
        parts = ["\n\n".join(parts[i : i + per]) for i in range(0, len(parts), per)]
    return parts


def build(task: str, tool_name: str, chunks: List[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    state = {
        "task": task[:3000],
        "tool": tool_name,
        "sections": {f"s{i}": c[:3000] for i, c in enumerate(chunks)},
    }
    qs = {
        f"keep_{i}": Noul(
            instructions=(
                f"Could `sections.s{i}` help complete `task`? Answer yes if it contains facts, code, "
                "data, errors, or references the task needs; no if it is navigation, boilerplate, "
                "ads, or unrelated content."
            )
        )
        for i in range(len(chunks))
    }
    return state, qs


def make_policy(cfg: Mapping[str, Any], n_chunks: int):
    keep_t = float(cfg.get("keep_threshold", 0.35))
    max_frac = float(cfg.get("max_kept_fraction", 0.85))

    def policy(a: Dict[str, Any]) -> Verdict:
        keep = [i for i in range(n_chunks) if a[f"keep_{i}"].noul >= keep_t]
        frac = len(keep) / max(1, n_chunks)
        detail = {"chunks": n_chunks, "kept": len(keep), "kept_idx": keep}
        if not keep:
            return Verdict("pass", {**detail, "reason": "nothing judged relevant; not trusting an empty filter"})
        if frac > max_frac:
            return Verdict("pass", {**detail, "reason": "almost everything relevant"})
        return Verdict("filter", detail)

    return policy


def render(chunks: List[str], kept_idx: List[int], wrapper: Optional[Dict[str, Any]], key: Optional[str],
           original_chars: int) -> str:
    kept = set(kept_idx)
    out: List[str] = []
    gap = 0
    for i, c in enumerate(chunks):
        if i in kept:
            if gap:
                out.append(ELIDED.format(n=gap))
                gap = 0
            out.append(c)
        else:
            gap += 1
    if gap:
        out.append(ELIDED.format(n=gap))
    text = "\n\n".join(out)
    note = (
        f"\n\n[jermes: kept {len(kept)}/{len(chunks)} sections, {len(text)}/{original_chars} chars. "
        "Re-run the tool or read the full file if an omitted section is needed.]"
    )
    if wrapper is not None and key is not None:
        new = dict(wrapper)
        new[key] = text + note
        return json.dumps(new, ensure_ascii=False)
    return text + note
