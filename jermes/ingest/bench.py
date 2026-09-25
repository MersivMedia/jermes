"""Ingestion benchmark: Jev pipeline vs one model reading every document.

Runs the same documents and fields through:

  jev      Jev pipeline (triage, screen, select, verify); flagged fields
           escalate to the strong model, everything else never touches it
  strong   the strong model reads each whole document and fills every field
  cheap    a cheap model does the same (optional)

and scores each against ground truth, with real token counts and list-price
cost for every model call (Jev at its published $0.042 per million input).
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..engine import Engine
from .candidates import NORMALIZERS
from .llm import ChatModel, Spend, extract_all, extractor
from .pipeline import Field, Pipeline, Schema

JEV_PER_M = 0.042


SEC_SCHEMA = Schema(
    name="sec_10k_cover",
    scope="an SEC annual report (Form 10-K) or its cover page",
    fields=[
        Field("state_of_incorporation", "In which US state or other jurisdiction is the company incorporated "
              "or organized?", "jurisdiction", required=True),
        Field("ein", "What is the company's IRS Employer Identification Number?", "code", required=True),
        Field("period_end", "What is the last day of the fiscal year this annual report covers?", "date",
              required=True),
        Field("file_number", "What is the company's SEC Commission File Number?", "code", required=True),
        Field("shares_outstanding", "How many shares of the company's common stock were outstanding as of the "
              "latest date given on the cover page?", "number", required=True),
        Field("public_float", "What was the aggregate market value of the voting stock held by non-affiliates "
              "(the public float) as of the last business day of the most recently completed second fiscal "
              "quarter, in dollars?", "money", required=True),
    ],
    doc_types=["10-K annual report", "10-Q quarterly report", "8-K current report", "proxy statement"],
)


# ---------------------------------------------------------------- scoring

def _canon(field: str, v: Any) -> Optional[str]:
    """Meaning, not characters: '1-3215' == '001-03215', 'December 31, 2025' == '2025-12-31'."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    if field == "file_number":
        m = re.search(r"0*(\d+)-0*(\d+)", s)
        return f"{int(m.group(1))}-{int(m.group(2))}" if m else s.lower()
    if field == "ein":
        d = re.sub(r"\D", "", s)
        return d or None
    if field in ("shares_outstanding", "public_float"):
        n = NORMALIZERS["number"](s) if not isinstance(v, (int, float)) else float(v)
        return f"{n:.6e}" if n else None   # 7 significant figures: "$3.25 trillion" != 3,253,431,000,000
    if field == "period_end":
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return s
        n = NORMALIZERS["date"](s)
        return n if isinstance(n, str) else s.lower()
    return re.sub(r"\s+", " ", s).lower().removeprefix("state of ").strip(" .")


@dataclass
class Score:
    right: int = 0
    wrong: int = 0      # a value that doesn't match truth (the costly kind)
    missing: int = 0    # returned nothing, or sent to review
    review: int = 0

    @property
    def n(self) -> int:
        return self.right + self.wrong + self.missing

    def as_dict(self) -> Dict[str, Any]:
        return {"right": self.right, "wrong": self.wrong, "missing": self.missing, "review": self.review,
                "accuracy": round(self.right / self.n, 3) if self.n else None}


def score(preds: Dict[str, Dict[str, Any]], truth: Dict[str, Dict[str, Any]], fields: List[str],
          review: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    total, per = Score(), {f: Score() for f in fields}
    wrong_list = []
    for doc, t in truth.items():
        p = preds.get(doc, {})
        for f in fields:
            if t.get(f) is None:          # no unambiguous ground truth for this doc
                continue
            gold, got = _canon(f, t.get(f)), _canon(f, p.get(f))
            s = per[f]
            if got is None:
                s.missing += 1
                total.missing += 1
                if review and f in review.get(doc, []):
                    s.review += 1
                    total.review += 1
            elif got == gold:
                s.right += 1
                total.right += 1
            else:
                s.wrong += 1
                total.wrong += 1
                wrong_list.append({"doc": doc, "field": f, "got": p.get(f), "truth": t.get(f)})
    return {"total": total.as_dict(), "fields": {f: s.as_dict() for f, s in per.items()}, "wrong": wrong_list}


# ---------------------------------------------------------------- runners

def run_jev(engine: Engine, schema: Schema, docs: Dict[str, str], strong: Optional[ChatModel],
            workers: int = 4) -> Dict[str, Any]:
    pipe = Pipeline(engine, schema, strong=extractor(strong) if strong else None)

    def one(item):
        doc_id, text = item
        return doc_id, pipe.run(doc_id, text)

    with ThreadPoolExecutor(workers) as ex:
        recs = dict(ex.map(one, docs.items()))
    preds, review = {}, {}
    usage = {"jev_calls": 0, "jev_tokens": 0, "cached": 0, "strong_calls": 0, "escalated_fields": 0}
    dispositions: Dict[str, int] = {}
    for doc_id, r in recs.items():
        preds[doc_id] = {n: (fr.span if fr.status == "accepted" else None) for n, fr in r.fields.items()}
        review[doc_id] = [n for n, fr in r.fields.items() if fr.status == "review"]
        for k in usage:
            usage[k] += int(r.usage.get(k, 0))
        dispositions[r.disposition] = dispositions.get(r.disposition, 0) + 1
    return {"preds": preds, "review": review, "usage": usage, "dispositions": dispositions,
            "records": {k: v.to_json() for k, v in recs.items()}}


def run_model(model: ChatModel, schema: Schema, docs: Dict[str, str], workers: int = 4) -> Dict[str, Any]:
    def one(item):
        doc_id, text = item
        try:
            return doc_id, extract_all(model, schema.fields, text)
        except Exception as exc:
            return doc_id, {"_error": str(exc)[:200]}

    with ThreadPoolExecutor(workers) as ex:
        preds = dict(ex.map(one, docs.items()))
    return {"preds": preds}


def benchmark(engine: Engine, docs: Dict[str, str], truth: Dict[str, Dict[str, Any]], *,
              strong_model: str, cheap_model: Optional[str] = None, schema: Schema = SEC_SCHEMA,
              arms: Optional[List[str]] = None, progress: Callable[[str], None] = print) -> Dict[str, Any]:
    arms = arms or ["jev", "strong"] + (["cheap"] if cheap_model else [])
    fields = [f.name for f in schema.fields]
    out: Dict[str, Any] = {"docs": len(docs), "fields": fields, "arms": {}}

    if "jev" in arms:
        spend = Spend()
        strong = ChatModel(strong_model, spend=spend)
        progress(f"jev pipeline ({len(docs)} docs, escalation -> {strong_model}) ...")
        r = run_jev(engine, schema, docs, strong)
        jev_usd = r["usage"]["jev_tokens"] * JEV_PER_M / 1e6
        out["arms"]["jev"] = {
            "score": score(r["preds"], truth, fields, r["review"]),
            "usage": r["usage"], "dispositions": r["dispositions"],
            "llm": spend.as_dict(), "jev_usd": round(jev_usd, 5),
            "usd": round(spend.usd + jev_usd, 5),
            "llm_input_tokens": spend.input_tokens, "llm_output_tokens": spend.output_tokens,
            "records": r["records"],
        }
    for arm, name in (("strong", strong_model), ("cheap", cheap_model)):
        if arm not in arms or not name:
            continue
        spend = Spend()
        progress(f"{arm}: {name} reads every document ...")
        r = run_model(ChatModel(name, spend=spend), schema, docs)
        out["arms"][arm] = {
            "model": name, "score": score(r["preds"], truth, fields),
            "llm": spend.as_dict(), "usd": round(spend.usd, 5),
            "llm_input_tokens": spend.input_tokens, "llm_output_tokens": spend.output_tokens,
            "errors": [d for d, p in r["preds"].items() if "_error" in p],
        }
    return out


def print_report(rep: Dict[str, Any], out=print) -> None:
    out("")
    out(f"  {rep['docs']} documents x {len(rep['fields'])} fields = {rep['docs'] * len(rep['fields'])} values")
    out(f"  {'arm':<8} {'right':>6} {'wrong':>6} {'missing':>8} {'review':>7} {'accuracy':>9}"
        f" {'LLM in tok':>11} {'LLM out':>8} {'$':>9}")
    for arm, a in rep["arms"].items():
        s = a["score"]["total"]
        out(f"  {arm:<8} {s['right']:>6} {s['wrong']:>6} {s['missing']:>8} {s['review']:>7} "
            f"{(s['accuracy'] or 0) * 100:>8.1f}% {a['llm_input_tokens']:>11,} {a['llm_output_tokens']:>8,} "
            f"{a['usd']:>9.4f}")
    j = rep["arms"].get("jev")
    if j:
        u = j["usage"]
        out(f"\n  jev: {u['jev_calls']} Jev requests, {u['jev_tokens']:,} Jev tokens (${j['jev_usd']:.4f}); "
            f"{u['escalated_fields']} fields escalated in {u['strong_calls']} strong-model calls; "
            f"dispositions {j['dispositions']}")
        st = rep["arms"].get("strong")
        if st and st["llm_input_tokens"]:
            cut = 1 - j["llm_input_tokens"] / st["llm_input_tokens"]
            out(f"  strong-model input tokens: {j['llm_input_tokens']:,} vs {st['llm_input_tokens']:,} "
                f"({cut * 100:.0f}% fewer); cost ${j['usd']:.4f} vs ${st['usd']:.4f}")
    for arm, a in rep["arms"].items():
        if a["score"]["wrong"]:
            out(f"\n  {arm} wrong values:")
            for w in a["score"]["wrong"][:12]:
                out(f"    {w['doc']:<28} {w['field']:<24} got {str(w['got'])[:30]!r:<32} truth {w['truth']!r}")


def load_bench(path: Path) -> tuple:
    truth = json.loads((path / "truth.json").read_text())
    docs = {k: (path / f"{k}.txt").read_text() for k in truth}
    return docs, truth
