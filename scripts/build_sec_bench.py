"""Build the SEC 10-K cover-page ingestion benchmark.

For each company: fetch its latest 10-K from EDGAR, convert the HTML to text,
keep the first ~25k characters (the cover page plus the start of Item 1, so
the model has to find the cover values among real noise), and record EDGAR's
structured values as ground truth.

Ground truth comes from EDGAR's submissions API, not from the document, so
the benchmark checks against an independent source:

  state_of_incorporation  submissions.stateOfIncorporation (2-letter code)
  ein                     submissions.ein (IRS employer id)
  period_end              filings.recent.reportDate of that 10-K
  fiscal_year_end         submissions.fiscalYearEnd (MMDD)
  file_number             filings.recent.fileNumber of that 10-K
  shares_outstanding      XBRL dei:EntityCommonStockSharesOutstanding for that filing
  public_float            XBRL dei:EntityPublicFloat for that filing (USD)

The last two sit among dozens of other numbers on the cover page and in
Item 1, which makes them the fields that separate careful extraction from
lucky extraction.

Usage: python scripts/build_sec_bench.py OUT_DIR [N] [--heldout]
SEC asks for a descriptive User-Agent with a contact address and <=10 req/s.
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
from pathlib import Path

import httpx

UA = "Mersiv Media research contact@mersivmedia.com"  # SEC format: name + email; URLs get 403

# Mixed sizes, industries and states of incorporation, so no single answer dominates.
CIKS = [
    "0000320193",  # Apple (CA)
    "0000789019",  # Microsoft (WA)
    "0001018724",  # Amazon (DE)
    "0000040545",  # GE
    "0000021344",  # Coca-Cola
    "0000200406",  # Johnson & Johnson (NJ)
    "0000080424",  # Procter & Gamble (OH)
    "0000066740",  # 3M
    "0000093410",  # Chevron
    "0000034088",  # Exxon Mobil (NJ)
    "0000019617",  # JPMorgan Chase
    "0000070858",  # Bank of America
    "0000732712",  # Verizon
    "0001065280",  # Netflix
    "0000354950",  # Home Depot
    "0000310158",  # Merck (NJ)
    "0000078003",  # Pfizer
    "0000027419",  # Target (MN)
    "0000063908",  # McDonald's
    "0000109198",  # TJX
    "0000829224",  # Starbucks (WA)
    "0000100885",  # Union Pacific (UT)
    "0000064803",  # CVS
    "0000773840",  # Honeywell
]

# Held out: never used while developing the finders or field questions.
HELDOUT = [
    "0000051143",  # IBM (NY)
    "0000050863",  # Intel
    "0000858877",  # Cisco
    "0000796343",  # Adobe
    "0001045810",  # NVIDIA
    "0000018230",  # Caterpillar
    "0000012927",  # Boeing
    "0000077476",  # PepsiCo (NC)
    "0000104169",  # Walmart
    "0000086312",  # Travelers (MN)
    "0000315189",  # Deere
    "0000217346",  # Textron
    "0000896878",  # Intuit
    "0000097745",  # Thermo Fisher
    "0000059478",  # Eli Lilly (IN)
    "0000037996",  # Ford
]

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "PR": "Puerto Rico", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|ix:header)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)>", "\n", raw)
    raw = re.sub(r"(?i)</t[dh]>", " \t ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    txt = html.unescape(raw).replace("\xa0", " ")
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r" +([,.;:)])", r"\1", txt)     # "December 31 , 2025" -> "December 31, 2025"
    txt = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", txt)
    return "\n".join(line.strip() for line in txt.splitlines()).strip()


def main(out: Path, n: int, ciks=None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    c = httpx.Client(headers={"User-Agent": UA}, timeout=60, follow_redirects=True)
    truth = {}
    for cik in ciks or CIKS:
        if len(truth) >= n:
            break
        resp = c.get(f"https://data.sec.gov/submissions/CIK{cik}.json")
        resp.raise_for_status()
        sub = resp.json()
        time.sleep(0.2)
        r = sub["filings"]["recent"]
        try:
            i = r["form"].index("10-K")
        except ValueError:
            continue
        acc = r["accessionNumber"][i]
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"
               f"{r['primaryDocument'][i]}")
        resp = c.get(url)
        time.sleep(0.2)
        if resp.status_code != 200:
            print("skip", sub["name"], resp.status_code)
            continue
        text = html_to_text(resp.text)
        # Start at the cover page ("UNITED STATES ... FORM 10-K"), skipping any XBRL preamble.
        m = re.search(r"UNITED STATES\s+SECURITIES AND EXCHANGE COMMISSION", text, re.I)
        text = text[m.start():] if m else text
        text = text[:25000]
        doc_id = re.sub(r"\W+", "_", sub["name"]).strip("_").lower()
        facts = c.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        time.sleep(0.2)
        dei = facts.json().get("facts", {}).get("dei", {}) if facts.status_code == 200 else {}

        def fact(name):
            vals = [r["val"] for u in dei.get(name, {}).get("units", {}).values() for r in u if r.get("accn") == acc]
            # Several share classes report separately; only a single value is unambiguous.
            return vals[0] if len(set(vals)) == 1 else None
        (out / f"{doc_id}.txt").write_text(text)
        st = sub.get("stateOfIncorporation") or ""
        ein = sub.get("ein") or ""
        truth[doc_id] = {
            "company": sub["name"],
            "url": url,
            "state_of_incorporation": STATES.get(st, st or None),
            "ein": f"{ein[:2]}-{ein[2:]}" if len(ein) == 9 else None,
            "period_end": r["reportDate"][i],
            "fiscal_year_end": sub.get("fiscalYearEnd"),
            "file_number": r["fileNumber"][i] or None,
            "shares_outstanding": fact("EntityCommonStockSharesOutstanding"),
            "public_float": fact("EntityPublicFloat"),
        }
        print(f"{doc_id:<32} {len(text):>6} chars  {truth[doc_id]['state_of_incorporation']}  "
              f"{truth[doc_id]['ein']}  {truth[doc_id]['period_end']}  {truth[doc_id]['file_number']}  "
              f"shares={truth[doc_id]['shares_outstanding']}  float={truth[doc_id]['public_float']}")
    (out / "truth.json").write_text(json.dumps(truth, indent=1))
    print(f"{len(truth)} documents -> {out}")


if __name__ == "__main__":
    heldout = "--heldout" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--heldout"]
    main(Path(args[0]), int(args[1]) if len(args) > 1 else 20, HELDOUT if heldout else None)
