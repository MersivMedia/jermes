"""Candidate finders for ingestion (PRD I4).

Code over-finds; Jev picks. Every finder returns ``Candidate`` objects that
point at an exact span of the source text, so whatever Jev selects is copied
verbatim from the document. It cannot be invented or have a digit transposed.

Finders are deliberately greedy: a missed candidate cannot be recovered by the
selector, while an extra one costs nothing but a slightly longer Choice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class Candidate:
    cid: str          # stable id shown to Jev ("c3")
    text: str         # exact source span
    start: int
    end: int
    kind: str = ""

    def context(self, source: str, width: int = 90) -> str:
        """The span with surrounding text, so Jev can tell which date/amount it is."""
        a, b = max(0, self.start - width), min(len(source), self.end + width)
        before = source[a:self.start].replace("\n", " ")
        after = source[self.end:b].replace("\n", " ")
        return f"...{before}[[{self.text}]]{after}..."


MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
          "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")

_PATTERNS: Dict[str, List[str]] = {
    "date": [
        rf"\b\d{{1,2}}\s+(?:{MONTHS})\.?,?\s+\d{{2,4}}\b",           # 19 June 1963
        rf"\b(?:{MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{2,4}}\b",  # June 19, 1963
        rf"\b(?:{MONTHS})\.?\s+\d{{4}}\b",                          # June 1963
        r"\b\d{4}-\d{2}-\d{2}\b",                                   # 1963-06-19
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",                             # 6/19/63
    ],
    "money": [
        r"(?:US)?\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:trillion|billion|million|thousand|[mbk]))?\b",
        r"\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|dollars|rubles|roubles)\b",
        # Large bare amounts ("non-affiliates as of June 30, 2025: 208,464,334,129."):
        # at least two thousands groups, or a scale word.
        r"(?<![\w.,$])\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?(?![\d,]*\d)",
        r"(?<![\w.,$])\d+(?:\.\d+)?\s(?:trillion|billion)\b",
    ],
    "email": [r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"],
    "phone": [r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)"],
    # A trailing sentence period or comma is not part of the number
    # ("710,398,642." -> "710,398,642"); a scale word is ("526.7 million").
    "number": [r"(?<![\w.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:\s(?:trillion|billion|million|thousand)\b)?(?![\d,]*\d)",
               r"(?<![\w.,])\d+(?:\.\d+)?(?:\s(?:trillion|billion|million|thousand)\b)?(?![\w]|[.,]\d)"],
    "size": [r"\b\d[\d,]*(?:\.\d+)?\s?(?:KB|MB|GB|TB|kB|bytes)\b"],
    "percent": [r"\b\d+(?:\.\d+)?\s?%"],
    "url": [r"https?://[^\s<>\"')\]]+"],
    # Hyphenated identifiers: tax ids (12-3456789), file numbers (001-36743).
    "code": [r"(?<![\w-])\d{1,3}-\d{3,9}(?![\w-])"],
    # Jurisdictions from a fixed list (a gazetteer is a finder too).
    "jurisdiction": [r"\b(?:" + "|".join([
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
        "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
        "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
        "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
        "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Puerto Rico",
        "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
        "Washington", "West Virginia", "Wisconsin", "Wyoming", "Bermuda", "Cayman Islands", "British Virgin Islands",
        "Ireland", "Israel", "Canada", "Netherlands", "Luxembourg", "Switzerland", "Jersey", "Marshall Islands",
        "England and Wales", "United Kingdom"]) + r")\b"],
    # Proper-noun runs: people, places, organisations. Over-finds on purpose.
    "name": [r"\b(?:[A-Z][a-z]+|[A-Z]\.)(?:\s+(?:[A-Z][a-z]+|[A-Z]\.|of|de|von|van|al)){0,4}\s+[A-Z][a-z]+\b",
             r"\b[A-Z]{2,}(?:\s+[A-Z]{2,}){0,4}\b"],
}

_COMPILED = {k: [re.compile(p, re.IGNORECASE if k in ("date", "money", "size") else 0) for p in v]
             for k, v in _PATTERNS.items()}


def find(text: str, kind: str, *, limit: int = 250) -> List[Candidate]:
    """All spans of ``kind`` in ``text``, de-duplicated by normalized value.

    The first occurrence of each distinct value wins, so the Choice never offers
    the same answer twice (which would split its probability).
    """
    pats = _COMPILED.get(kind)
    if pats is None:
        raise ValueError(f"unknown candidate kind {kind!r}; known: {sorted(_COMPILED)}")
    hits = []
    for pat in pats:
        for m in pat.finditer(text):
            hits.append((m.start(), m.end(), m.group(0).strip()))
    hits.sort()
    # Drop spans contained in an earlier, longer span ("June 1963" inside "19 June 1963").
    kept: List[tuple] = []
    for s, e, val in hits:
        if kept and s >= kept[-1][0] and e <= kept[-1][1]:
            continue
        kept.append((s, e, val))
    out: List[Candidate] = []
    seen = set()
    for s, e, val in kept:
        key = re.sub(r"\W+", "", val.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(Candidate(f"c{len(out) + 1}", val, s, e, kind))
        if len(out) >= limit:
            break
    return out


def _locate(text: str, value: str) -> Optional[tuple]:
    """(start, end) of ``value`` in ``text``: exact, then case-insensitive, then
    whitespace-insensitive ("$3.6 trillion" finds "$ 3.6 trillion")."""
    i = text.find(value)
    if i >= 0:
        return i, i + len(value)
    i = text.lower().find(value.lower())
    if i >= 0:
        return i, i + len(value)
    parts = [re.escape(p) for p in re.findall(r"\w+|[^\w\s]", value)]
    if not parts:
        return None
    m = re.search(r"\s*".join(parts), text, re.IGNORECASE)
    return (m.start(), m.end()) if m else None


def from_list(text: str, values: Iterable[str], kind: str = "given") -> List[Candidate]:
    """Candidates proposed elsewhere (a cheap LLM, a NER model, a lookup table).

    Only values that occur in ``text`` survive (spacing and case may differ;
    the returned span is always the document's own text), so a generator that
    hallucinates cannot smuggle a value past the selector.
    """
    out: List[Candidate] = []
    seen = set()
    for v in values:
        v = (v or "").strip()
        if not v or v.lower() in seen:
            continue
        loc = _locate(text, v)
        if loc is None:
            continue
        seen.add(v.lower())
        s, e = loc
        out.append(Candidate(f"c{len(out) + 1}", text[s:e], s, e, kind))
    return out


# ---------------------------------------------------------------- normalizers

_MONTH_NUM = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
_MONTH_NUM.update({k[:3]: v for k, v in list(_MONTH_NUM.items())})
_MONTH_NUM["sept"] = 9


def normalize_date(s: str) -> Optional[str]:
    """ISO date (or YYYY-MM when no day) from the span; None if unparseable.

    Two-digit years are left unresolved rather than guessed.
    """
    t = s.strip().lower().replace(",", " ").replace(".", " ")
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return t
    m = re.fullmatch(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", t)
    if m and m.group(2)[:3] in _MONTH_NUM:
        return f"{m.group(3)}-{_MONTH_NUM[m.group(2)[:3]]:02d}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{4})", t)
    if m and m.group(1)[:3] in _MONTH_NUM:
        return f"{m.group(3)}-{_MONTH_NUM[m.group(1)[:3]]:02d}-{int(m.group(2)):02d}"
    m = re.fullmatch(r"([a-z]+)\s+(\d{4})", t)
    if m and m.group(1)[:3] in _MONTH_NUM:
        return f"{m.group(2)}-{_MONTH_NUM[m.group(1)[:3]]:02d}"
    return None


def normalize_number(s: str) -> Optional[float]:
    """Assumes comma thousands separators, as the TypeSafe cookbook does."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", s)
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    low = s.lower()
    for word, mult in (("trillion", 1e12), ("billion", 1e9), ("million", 1e6), ("thousand", 1e3)):
        if word in low:
            v *= mult
    return v


NORMALIZERS: Dict[str, Callable[[str], object]] = {
    "date": normalize_date,
    "money": normalize_number,
    "number": normalize_number,
    "percent": normalize_number,
    "size": lambda s: s.strip(),
    "email": lambda s: s.strip().lower(),
    "phone": lambda s: re.sub(r"[^\d+]", "", s),
    "url": lambda s: s.strip(),
    "code": lambda s: s.strip(),
    "jurisdiction": lambda s: " ".join(s.split()),
    "name": lambda s: " ".join(s.split()),
    "given": lambda s: " ".join(s.split()),
}
