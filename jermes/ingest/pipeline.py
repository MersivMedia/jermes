"""Ingestion pipeline on Jev (PRD section 6).

Stages, each a separate Jev request so they can be cached and inspected:

  I2 triage      one request per document: in scope, type, language, quality
  I3 screen      one request per document: 4 Nouls per chunk (relevant,
                 has evidence, contradicts the stated premise, contains an
                 instruction). Code decides evidence / conflict / quarantine / drop.
  I4 candidates  code (candidates.py) or a cheap model proposes spans
  I5 select      one Choice per field over the candidate ids + "not stated"
  I6 verify      per-field Nouls where "true" means something is wrong
  I7 escalate    fields with any flag >= threshold go to a strong model (or
                 review when none is configured); nothing else does
  I8 classify    hierarchical Choice; low confidence reports the parent
  I9 review      everything uncertain lands in the review list, never silently

Jev never writes a value. Every accepted value is an exact span of the
document, copied and normalized by code. The record keeps every Jev answer
and the policy that turned it into a disposition, so any value can be traced.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..engine import Engine, Verdict
from ..questions import Choice, Noul
from .candidates import NORMALIZERS, Candidate, find, from_list

SPEC_VERSION = "ingest.1"
NONE = "not_stated"
POINT = "ingest"


# ---------------------------------------------------------------- schema

@dataclass
class Field:
    """One value to extract.

    ``kind`` picks the candidate finder (date, money, email, phone, number,
    percent, size, url, name). ``kind="given"`` expects candidates from a
    generator (``Pipeline.propose``). ``question`` is what Jev is asked.
    """

    name: str
    question: str
    kind: str
    required: bool = False


@dataclass
class Taxonomy:
    """Two-level taxonomy for I8: {parent: {child: description}}."""

    levels: Dict[str, Dict[str, str]]
    question: str = "Which category best describes this document?"


@dataclass
class Schema:
    name: str
    scope: str                       # what counts as in scope, in one sentence
    fields: List[Field]
    doc_types: List[str] = field(default_factory=list)
    taxonomy: Optional[Taxonomy] = None
    premise: str = ""                # optional claim the corpus is being read against


# ---------------------------------------------------------------- results

@dataclass
class FieldResult:
    name: str
    value: Any = None                # normalized value, or None
    span: Optional[str] = None       # exact source text
    offset: Optional[int] = None
    status: str = "pending"          # accepted | not_stated | review | escalated | no_candidates
    p: float = 0.0                   # Jev's probability for the pick
    flags: Dict[str, float] = field(default_factory=dict)
    source: str = "jev"              # jev | strong_model | none
    candidates: int = 0


@dataclass
class ChunkResult:
    index: int
    route: str                       # evidence | conflict | quarantine | drop
    scores: Dict[str, float]
    chars: int


@dataclass
class Record:
    doc_id: str
    schema: str
    disposition: str = "pending"     # accepted | review | rejected | error
    triage: Dict[str, Any] = field(default_factory=dict)
    category: Dict[str, Any] = field(default_factory=dict)
    fields: Dict[str, FieldResult] = field(default_factory=dict)
    chunks: List[ChunkResult] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    def values(self) -> Dict[str, Any]:
        return {k: f.value for k, f in self.fields.items()}


# ---------------------------------------------------------------- helpers

def chunk_text(text: str, size: int) -> List[tuple]:
    """(start, chunk) pairs on paragraph boundaries, falling back to hard cuts."""
    paras = re.split(r"(\n\s*\n)", text)
    chunks: List[tuple] = []
    buf, start, pos = "", 0, 0
    for piece in paras:
        if len(buf) + len(piece) > size and buf.strip():
            chunks.append((start, buf))
            buf, start = "", pos
        while len(piece) > size:
            chunks.append((pos, piece[:size]))
            piece, pos = piece[size:], pos + size
            start = pos
        buf += piece
        pos += len(piece)
    if buf.strip():
        chunks.append((start, buf))
    return chunks


def _p(ans: Any) -> float:
    return float(getattr(ans, "probability", 0.0) or 0.0)


def batches(items: Sequence[Any], cost: Callable[[Any], int], budget: int) -> List[List[Any]]:
    """Greedy split so each request's state stays under ``budget`` characters.

    Jev accepts about 32k tokens per request (state plus questions); one item
    larger than the budget still gets its own request.
    """
    out: List[List[Any]] = []
    cur: List[Any] = []
    used = 0
    for it in items:
        c = cost(it)
        if cur and used + c > budget:
            out.append(cur)
            cur, used = [], 0
        cur.append(it)
        used += c
    if cur:
        out.append(cur)
    return out


# A strong-model extractor: (fields, document_text) -> {field name: value or None}.
# Called once per record with every flagged field.
Extractor = Callable[[List[Field], str], Dict[str, Optional[str]]]
# A candidate generator: (field, document_text) -> list of proposed strings.
Proposer = Callable[[Field, str], List[str]]


# ---------------------------------------------------------------- pipeline

class Pipeline:
    def __init__(self, engine: Engine, schema: Schema, *, strong: Optional[Extractor] = None,
                 propose: Optional[Proposer] = None) -> None:
        self.engine = engine
        self.schema = schema
        self.cfg = engine.point_config(POINT)
        self.strong = strong
        self.propose = propose

    # -- one Jev request, logged under ingest.<stage> --------------------

    def _ask(self, stage: str, doc_id: str, state: Any, questions: Mapping[str, Any],
             usage: Dict[str, Any]) -> Dict[str, Any]:
        d = self.engine.decide(f"{POINT}.{stage}", state, questions, lambda a: Verdict("answered"),
                               session_id=f"ingest:{doc_id}", spec_version=SPEC_VERSION)
        usage["jev_calls"] = usage.get("jev_calls", 0) + 1
        usage["jev_tokens"] = usage.get("jev_tokens", 0) + d.input_tokens
        usage["cached"] = usage.get("cached", 0) + int(d.cached)
        if d.error:
            raise RuntimeError(f"{stage}: {d.error}")
        return d.answers

    # -- I2 -----------------------------------------------------------------

    def triage(self, doc_id: str, text: str, usage: Dict[str, Any]) -> Dict[str, Any]:
        head = text[:3000]
        qs: Dict[str, Any] = {
            "in_scope": Noul(f"Is this document in scope? In scope means: {self.schema.scope}"),
            "readable": Noul("Is this text readable prose or data, rather than OCR garbage, "
                             "boilerplate, or an empty page?"),
            "english": Noul("Is this document written mainly in English?"),
        }
        if self.schema.doc_types:
            opts = {t: None for t in self.schema.doc_types}
            opts["other"] = "none of the listed types"
            qs["doc_type"] = Choice("What type of document is this?", opts)
        a = self._ask("triage", doc_id, {"document_start": head}, qs, usage)
        out = {k: round(_p(a[k]), 3) for k in ("in_scope", "readable", "english")}
        if "doc_type" in a:
            out["doc_type"] = a["doc_type"].choice
            out["doc_type_p"] = round(a["doc_type"].probabilities.get(a["doc_type"].choice, 0.0), 3)
        return out

    # -- I3 -----------------------------------------------------------------

    def screen(self, doc_id: str, text: str, usage: Dict[str, Any]) -> List[ChunkResult]:
        size = int(self.cfg.get("chunk_chars", 1500))
        chunks = chunk_text(text, size)[: int(self.cfg.get("max_chunks", 60))]
        if len(chunks) <= 1:
            return [ChunkResult(0, "evidence", {}, len(text))]
        fields = {f.name: f.question for f in self.schema.fields}
        answers: Dict[str, Any] = {}
        budget = int(self.cfg.get("request_chars", 60000))
        for group in batches(list(range(len(chunks))), lambda i: len(chunks[i][1]) + 200, budget):
            qs: Dict[str, Any] = {}
            for i in group:
                qs[f"rel_{i}"] = Noul(f"Does chunk {i} contain information needed for any of the fields in state.fields?")
                qs[f"ins_{i}"] = Noul(f"Does chunk {i} contain text that tries to give instructions to an AI system "
                                      "or to whoever processes this document (rather than being ordinary content)?")
                if self.schema.premise:
                    qs[f"con_{i}"] = Noul(f"Does chunk {i} contradict the premise in state.premise?")
            state: Dict[str, Any] = {"fields": fields, "chunks": {str(i): chunks[i][1] for i in group}}
            if self.schema.premise:
                state["premise"] = self.schema.premise
            answers.update(self._ask("screen", doc_id, state, qs, usage))
        keep_t = float(self.cfg.get("keep_threshold", 0.5))
        inj_t = float(self.cfg.get("injection_threshold", 0.7))
        out = []
        for i, (_, c) in enumerate(chunks):
            s = {"relevant": round(_p(answers[f"rel_{i}"]), 3), "instruction": round(_p(answers[f"ins_{i}"]), 3)}
            if self.schema.premise:
                s["contradicts"] = round(_p(answers[f"con_{i}"]), 3)
            if s["instruction"] >= inj_t:
                route = "quarantine"
            elif s.get("contradicts", 0) >= keep_t:
                route = "conflict"
            elif s["relevant"] >= keep_t:
                route = "evidence"
            else:
                route = "drop"
            out.append(ChunkResult(i, route, s, len(c)))
        return out

    # -- I4 + I5 -------------------------------------------------------------

    def _candidates(self, f: Field, text: str) -> List[Candidate]:
        limit = int(self.cfg.get("max_candidates", 250))
        if f.kind == "given":
            if not self.propose:
                return []
            return from_list(text, self.propose(f, text))[:limit]
        return find(text, f.kind, limit=limit)

    def select(self, doc_id: str, text: str, usage: Dict[str, Any], focus: Optional[str] = None) -> Dict[str, tuple]:
        """A Choice per field over its candidate ids plus "not stated", batched under the budget.

        ``focus`` is ``text`` with non-evidence chunks blanked (same offsets).
        Candidates come from ``focus`` first. A field with no candidates there,
        or whose answer over them is "not stated", is asked again over the
        whole document: a chunk the screen wrongly dropped then costs one
        more request instead of a lost value.
        """
        if focus is None:
            return self._select(doc_id, text, {f.name: self._candidates(f, text) for f in self.schema.fields}, usage)
        first = self._select(doc_id, text, {f.name: self._candidates(f, focus) for f in self.schema.fields}, usage)
        retry = {}
        for f in self.schema.fields:
            cs, ans = first[f.name]
            if cs and ans is not None and ans.choice != NONE:
                continue
            full = self._candidates(f, text)
            if len(full) > len(cs):
                retry[f.name] = full
        if retry:
            usage["focus_fallbacks"] = usage.get("focus_fallbacks", 0) + len(retry)
            first.update(self._select(doc_id, text, retry, usage))
        return first

    def _select(self, doc_id: str, text: str, cands: Dict[str, List[Candidate]],
                usage: Dict[str, Any]) -> Dict[str, tuple]:
        budget = int(self.cfg.get("request_chars", 60000))
        by = {f.name: f for f in self.schema.fields}
        ctx: Dict[str, Dict[str, str]] = {}
        for name in list(cands):
            shown, used = {}, 0
            for c in cands[name]:
                s = c.context(text)
                if used + len(s) > budget:   # one huge field: keep the earliest candidates
                    break
                shown[c.cid] = s
                used += len(s)
            cands[name] = [c for c in cands[name] if c.cid in shown]
            ctx[name] = shown
        todo = [by[n] for n in cands if cands[n]]
        answers: Dict[str, Any] = {}
        for group in batches(todo, lambda f: sum(len(s) for s in ctx[f.name].values()) + 300, budget):
            qs: Dict[str, Any] = {}
            state: Dict[str, Any] = {"candidates": {}}
            for f in group:
                opts = {cid: None for cid in ctx[f.name]}
                opts[NONE] = "the document does not state this, or none of the candidates is it"
                qs[f"pick_{f.name}"] = Choice(
                    f"{f.question} Each option is a candidate id; the candidate text in context is under "
                    f"candidates.{f.name}, marked with [[ ]]. Pick the id that answers the question, or {NONE}.", opts)
                state["candidates"][f.name] = ctx[f.name]
            answers.update(self._ask("select", doc_id, state, qs, usage))
        return {n: (cands[n], answers.get(f"pick_{n}")) for n in cands}

    # -- I6 -----------------------------------------------------------------

    def verify(self, doc_id: str, text: str, picked: Dict[str, FieldResult],
               usage: Dict[str, Any]) -> None:
        todo = {n: r for n, r in picked.items() if r.span is not None}
        if not todo:
            return
        by = {f.name: f for f in self.schema.fields}
        qs: Dict[str, Any] = {}
        state: Dict[str, Any] = {"extracted": {}}
        for n, r in todo.items():
            f = by[n]
            ctx = Candidate("x", r.span, r.offset or 0, (r.offset or 0) + len(r.span)).context(text, 160)
            state["extracted"][n] = {"question": f.question, "value": r.span, "where": ctx}
            qs[f"wrong_{n}"] = Noul(
                f"Look at extracted.{n}. Is this value the wrong answer to its question, for example "
                "a different date, amount, or name mentioned nearby, or text that does not answer the question?")
            qs[f"unrelated_{n}"] = Noul(
                f"Look at extracted.{n}. Does the surrounding text show this value belongs to an unrelated "
                "passage (a quoted example, a footnote, a different document or subject)?")
        a = self._ask("verify", doc_id, state, qs, usage)
        for n, r in todo.items():
            r.flags = {"wrong": round(_p(a[f"wrong_{n}"]), 3), "unrelated": round(_p(a[f"unrelated_{n}"]), 3)}

    # -- I8 -----------------------------------------------------------------

    def classify(self, doc_id: str, text: str, usage: Dict[str, Any]) -> Dict[str, Any]:
        tax = self.schema.taxonomy
        if not tax:
            return {}
        head = text[:4000]
        parents = {p: ", ".join(sorted(ch)) for p, ch in tax.levels.items()}
        a = self._ask("classify", doc_id, {"document_start": head},
                      {"parent": Choice(tax.question + " Choose the broad group.", parents)}, usage)
        parent = a["parent"].choice
        p_conf = a["parent"].probabilities.get(parent, 0.0)
        children = tax.levels.get(parent, {})
        out: Dict[str, Any] = {"parent": parent, "parent_p": round(p_conf, 3)}
        if len(children) >= 2:
            b = self._ask("classify", doc_id, {"document_start": head, "group": parent},
                          {"child": Choice(tax.question + f" It is in the group '{parent}'. Choose the specific category.",
                                           dict(children))}, usage)
            child = b["child"].choice
            c_conf = b["child"].probabilities.get(child, 0.0)
            out.update({"child": child, "child_p": round(c_conf, 3)})
        min_c = float(self.cfg.get("class_min_confidence", 0.9))
        if out.get("child") and out["child_p"] >= min_c:
            out["label"], out["depth"] = out["child"], 2
        else:
            out["label"], out["depth"] = parent, 1   # report the parent rather than guess
        return out

    # -- the run -------------------------------------------------------------

    def run(self, doc_id: str, text: str) -> Record:
        rec = Record(doc_id, self.schema.name)
        usage: Dict[str, Any] = {"doc_chars": len(text)}
        t0 = time.monotonic()
        try:
            rec.triage = self.triage(doc_id, text, usage)
            if rec.triage["in_scope"] < 0.5 or rec.triage["readable"] < 0.5:
                rec.disposition = "rejected"
                rec.reasons.append("out of scope" if rec.triage["in_scope"] < 0.5 else "unreadable")
                return rec
            if rec.triage["english"] < 0.5:
                rec.reasons.append("non-English: held to review until tested")

            rec.chunks = self.screen(doc_id, text, usage)
            quarantined = [c.index for c in rec.chunks if c.route == "quarantine"]
            if quarantined:
                rec.reasons.append(f"embedded instructions in chunk(s) {quarantined}")
            # Candidates come from the whole document except quarantined chunks,
            # so a planted instruction can't supply a value.
            work = text
            if quarantined:
                spans = chunk_text(text, int(self.cfg.get("chunk_chars", 1500)))
                for i in quarantined:
                    s, c = spans[i]
                    work = work[:s] + " " * len(c) + work[s + len(c):]

            # I3 narrows I5: candidates come from evidence/conflict chunks. A field
            # with no candidate there falls back to the whole document, so a chunk
            # the screen wrongly dropped costs a longer Choice, not a lost value.
            focus = None
            if len(rec.chunks) > 1:
                spans = chunk_text(text, int(self.cfg.get("chunk_chars", 1500)))
                keep = {c.index for c in rec.chunks if c.route in ("evidence", "conflict")}
                if keep:
                    # Built from ``work``, so quarantined text stays blank; offsets match ``text``.
                    focus = "".join(work[s:s + len(c)] if i in keep else " " * len(c)
                                    for i, (s, c) in enumerate(spans))
                    focus += " " * (len(work) - len(focus))
                usage["evidence_chunks"] = len(keep)
            picks = self.select(doc_id, work, usage, focus=focus)
            pick_t = float(self.cfg.get("pick_min_p", 0.6))
            none_t = float(self.cfg.get("none_min_p", 0.6))
            for f in self.schema.fields:
                cs, ans = picks[f.name]
                r = FieldResult(f.name, candidates=len(cs))
                rec.fields[f.name] = r
                if not cs:
                    r.status, r.source = "no_candidates", "none"
                    continue
                choice = ans.choice
                r.p = round(ans.probabilities.get(choice, 0.0), 3)
                if choice == NONE:
                    r.status = "not_stated" if r.p >= none_t else "review"
                    continue
                c = next(x for x in cs if x.cid == choice)
                r.span, r.offset = c.text, c.start
                norm = NORMALIZERS.get(f.kind, lambda s: s)(c.text)
                r.value = norm if norm is not None else c.text
                r.status = "accepted" if r.p >= pick_t else "review"

            self.verify(doc_id, work, rec.fields, usage)
            flag_t = float(self.cfg.get("flag_threshold", 0.7))
            flagged: List[Field] = []
            for f in self.schema.fields:
                r = rec.fields[f.name]
                worst = max(r.flags.values(), default=0.0)   # max, not mean: one red flag escalates
                if worst >= flag_t or (f.required and r.status in ("not_stated", "no_candidates", "review")):
                    r.status = "escalated"
                    flagged.append(f)
            usage["escalated_fields"] = len(flagged)
            if flagged and self.strong:
                usage["strong_calls"] = usage.get("strong_calls", 0) + 1
                got = self.strong(flagged, work) or {}
                for f in flagged:
                    r = rec.fields[f.name]
                    v = got.get(f.name)
                    back = from_list(work, [v]) if v else []
                    r.source = "strong_model"
                    if back:   # the strong model's answer must also be a real span
                        c = back[0]
                        r.span, r.offset = c.text, c.start
                        norm = NORMALIZERS.get(f.kind, lambda s: s)(c.text)
                        r.value = norm if norm is not None else c.text
                        r.status = "accepted"
                    else:
                        r.value, r.span, r.offset = None, None, None
                        r.status = "not_stated" if not v else "review"   # invented value: a human looks
            else:
                for f in flagged:
                    rec.fields[f.name].status = "review"

            rec.category = self.classify(doc_id, work, usage)

            if any(r.status == "review" for r in rec.fields.values()) or rec.reasons:
                rec.disposition = "review"
            else:
                rec.disposition = "accepted"
        except Exception as exc:  # a failed record goes to review with the reason, never silently
            rec.disposition = "error"
            rec.reasons.append(str(exc)[:300])
        finally:
            usage["seconds"] = round(time.monotonic() - t0, 2)
            rec.usage = usage
        return rec


def load_schema(data: Mapping[str, Any]) -> Schema:
    """Schema from a dict (YAML/JSON file)."""
    tax = data.get("taxonomy")
    return Schema(
        name=data["name"],
        scope=data["scope"],
        fields=[Field(**f) for f in data["fields"]],
        doc_types=list(data.get("doc_types") or []),
        taxonomy=Taxonomy(levels=tax["levels"], question=tax.get("question", Taxonomy.question))
        if tax else None,
        premise=data.get("premise", ""),
    )
