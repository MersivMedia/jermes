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

SPEC_VERSION = "result_filter.2"
_LINE_PREFIX = re.compile(r"^\s*(\d+)\|", re.M)   # Hermes read_file: "   123|text"


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


def _is_blank(line: str) -> bool:
    """Blank, including Hermes read_file's numbered blank lines ("   311|")."""
    return not _LINE_PREFIX.sub("", line, count=1).strip()


def chunk(text: str, max_chunks: int) -> List[str]:
    """Split on blank lines; merge small pieces so we stay under max_chunks.

    Blank lines are recognised even when read_file numbers them, so a
    Markdown section stays with its heading instead of being cut every few lines.
    """
    parts: List[str] = []
    cur: List[str] = []
    for line in text.splitlines():
        if _is_blank(line):
            if cur:
                parts.append("\n".join(cur))
                cur = []
        else:
            cur.append(line)
    if cur:
        parts.append("\n".join(cur))
    # Blocks much larger than an even share (a transcript or log with no blank
    # lines) are cut by line count, so one block can't hold most of the text.
    target = max(1, len(text) // max_chunks)
    split: List[str] = []
    for p in parts:
        if len(p) <= max(3 * target, 2000):
            split.append(p)
            continue
        lines = p.splitlines()
        per = max(1, int(len(lines) * target / max(1, len(p))))
        split.extend("\n".join(lines[i : i + per]) for i in range(0, len(lines), per))
    parts = split
    if len(parts) > max_chunks:
        per = len(parts) // max_chunks + 1
        parts = ["\n\n".join(parts[i : i + per]) for i in range(0, len(parts), per)]
    return parts


_HEADING = re.compile(r"^(?:\s*\d+\|)?(#{1,6} .+|[A-Z][A-Z0-9 ,.&'()/-]{4,80})$")


def heading_of(chunks: List[str], i: int) -> Optional[str]:
    """Nearest heading at or above chunk ``i`` (Markdown "#" or an ALL-CAPS line)."""
    for j in range(i, -1, -1):
        for line in reversed(chunks[j].splitlines() if j < i else chunks[j].splitlines()[:1]):
            m = _HEADING.match(line.rstrip())
            if m:
                return line.strip()
    return None


def targeted(tool_name: str, args: Mapping[str, Any]) -> bool:
    """True when the call already asks for a specific part of the output.

    A ``read_file`` with an explicit offset or a short limit, or a command
    already piped through head/tail/sed/grep/awk, is the agent narrowing the
    output itself. Filtering it again second-guesses that choice, and when a
    model re-reads an omitted range to check it, filtering the re-read hides
    the very lines it asked for.
    """
    if tool_name == "read_file":
        off = args.get("offset")
        lim = args.get("limit")
        try:
            if off not in (None, "", 0, 1, "1"):
                return True
            if lim not in (None, "") and int(lim) < 2000:
                return True
        except (TypeError, ValueError):
            return False
    if tool_name == "terminal":
        cmd = str(args.get("command") or "")
        if re.search(r"\|\s*(head|tail|sed|grep|rg|awk|cut|jq|wc)\b", cmd) or re.match(r"\s*(head|tail|sed -n|grep|rg)\b", cmd):
            return True
    return False


def build(task: str, tool_name: str, chunks: List[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    state = {
        "task": task[:3000],
        "tool": tool_name,
        "sections": {f"s{i}": c[:3000] for i, c in enumerate(chunks)},
    }
    qs: Dict[str, Any] = {
        f"keep_{i}": Noul(
            instructions=(
                f"Could `sections.s{i}` help complete `task`? Answer yes if it contains facts, code, "
                "data, errors, or references the task needs; no if it is navigation, boilerplate, "
                "ads, or unrelated content."
            )
        )
        for i in range(len(chunks))
    }
    # Asked in the same request. When the user wants the whole thing read,
    # summarised, reviewed or translated, trimming works against them.
    qs["needs_all"] = Noul(
        instructions=(
            "Does `task` require the complete content of this output (for example: read all of it, summarise "
            "or review the whole thing, translate it, check every entry), rather than finding specific "
            "information in it?"
        )
    )
    return state, qs


def make_policy(cfg: Mapping[str, Any], n_chunks: int):
    keep_t = float(cfg.get("keep_threshold", 0.35))
    max_frac = float(cfg.get("max_kept_fraction", 0.85))
    all_t = float(cfg.get("needs_all_threshold", 0.6))

    def policy(a: Dict[str, Any]) -> Verdict:
        keep = [i for i in range(n_chunks) if a[f"keep_{i}"].noul >= keep_t]
        frac = len(keep) / max(1, n_chunks)
        needs_all = a["needs_all"].noul if "needs_all" in a else 0.0
        detail = {"chunks": n_chunks, "kept": len(keep), "kept_idx": keep, "needs_all": round(needs_all, 3)}
        if needs_all >= all_t:
            return Verdict("pass", {**detail, "reason": "the task needs the complete output"})
        if not keep:
            return Verdict("pass", {**detail, "reason": "nothing judged relevant; not trusting an empty filter"})
        if frac > max_frac:
            return Verdict("pass", {**detail, "reason": "almost everything relevant"})
        return Verdict("filter", detail)

    return policy


def line_spans(text: str, chunks: List[str]) -> List[Tuple[int, int]]:
    """(first, last) line of each chunk, 1-based.

    For ``read_file`` output (lines prefixed "   N|") these are the file's own
    line numbers; otherwise they are line numbers in the saved full copy.
    """
    spans: List[Tuple[int, int]] = []
    cursor = 0
    for c in chunks:
        nums = [int(m) for m in _LINE_PREFIX.findall(c)]
        if nums:
            spans.append((nums[0], nums[-1]))
            continue
        head = c.strip()[:80]
        pos = text.find(head, cursor) if head else -1
        if pos < 0:
            pos = cursor
        first = text.count("\n", 0, pos) + 1
        spans.append((first, first + c.strip().count("\n")))
        cursor = pos + max(1, len(head))
    return spans


def _range(spans: List[Tuple[int, int]], a: int, b: int) -> str:
    lo, hi = spans[a][0], spans[b][1]
    return f"line {lo}" if lo == hi else f"lines {lo}-{hi}"


def render(chunks: List[str], kept_idx: List[int], wrapper: Optional[Dict[str, Any]], key: Optional[str],
           original_chars: int, *, text: Optional[str] = None, saved_path: Optional[str] = None,
           task: str = "") -> str:
    """The filtered result the model sees.

    Written so the model can trust it: every cut says which lines it covers,
    the full output's location is given, and the note says how to check one
    detail cheaply instead of re-reading everything.
    """
    kept = set(kept_idx)
    spans = line_spans(text if text is not None else "\n\n".join(chunks), chunks)
    out: List[str] = []
    omitted: List[str] = []
    gap_start: Optional[int] = None
    for i, c in enumerate(chunks + [None]):  # sentinel flushes a trailing gap
        if c is not None and i not in kept:
            if gap_start is None:
                gap_start = i
            continue
        if gap_start is not None:
            r = _range(spans, gap_start, i - 1)
            omitted.append(r)
            n = i - gap_start
            out.append(f"[... {r} omitted ({n} section{'s' if n > 1 else ''} judged not relevant to the task) ...]")
            gap_start = None
        if c is not None:
            h = heading_of(chunks, i)
            if h and h not in c.splitlines()[0]:
                c = f"(under: {h})\n{c}"
            out.append(c)
    body = "\n\n".join(out)
    task_line = " ".join(task.split())[:160]
    ranges = ", ".join(omitted[:12]) + (" ..." if len(omitted) > 12 else "")
    note = (
        f"\n\n[jermes: {len(kept)} of {len(chunks)} sections shown ({len(body):,} of {original_chars:,} characters), "
        + (f'screened for relevance to "{task_line}". ' if task_line else "screened for relevance. ")
        + f"Omitted: {ranges}."
        + (f" Full output: {saved_path}." if saved_path else "")
        + " If the task needs something you don't see here, read just that range"
        + (" (read_file with offset and limit)" if saved_path or _LINE_PREFIX.search(body) else "")
        + "; reads of a specific range are never screened.]"
    )
    if wrapper is not None and key is not None:
        new = dict(wrapper)
        new[key] = body + note
        return json.dumps(new, ensure_ascii=False)
    return body + note


def save_full(text: str, directory: Any, keep: int = 200) -> Optional[str]:
    """Write the unfiltered text where the model can read it; prune old copies.

    Owner-only permissions: tool output can contain anything the tool read.
    """
    import hashlib
    import os
    from pathlib import Path

    try:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:16]}.txt"
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(text)
        old = sorted(d.glob("*.txt"), key=lambda p: p.stat().st_mtime)
        for p in old[:-keep]:
            p.unlink(missing_ok=True)
        return str(path)
    except OSError:
        return None
