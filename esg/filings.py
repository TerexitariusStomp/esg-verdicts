"""filings.py — SEC EDGAR 10-K/20-F climate-risk extraction via edgartools.

The filed-claims lane for companies with no standalone CSR report — and the
higher-stakes document for litigation anyway. Fetches the 10-K, extracts the
climate-relevant sections, runs the same regex tier into `extractions`
with kind='10-K' via a synthetic reports row.

EDGAR identity is required by the API (set_identity) — use a research UA.
"""
import argparse
import os
import re

from .esg import db, now
from .extract import regex_extract

try:
    from edgar import Company, set_identity
    HAVE_EDGAR = True
except Exception:
    HAVE_EDGAR = False

CLIMATE_RE = re.compile(
    r"climate|emissions?|greenhouse|carbon|scope\s*[123]|ghg|net[\s-]?zero|"
    r"renewable|energy\s+transition|environmental", re.I)


def _identity():
    ident = os.environ.get("EDGAR_IDENTITY",
                           "esg-verdicts research ops@esg-verdicts.example")
    set_identity(ident)


def sections_with_climate(filing):
    """Return list of (section_name, text) from a 10-K object that mention
    climate terms. edgartools gives section text; fall back to full text."""
    out = []
    try:
        obj = filing.obj()
        for attr in ("item1", "item1a", "item7", "item8"):
            t = getattr(obj, attr, None) or getattr(obj, attr.upper(), None)
            if t and isinstance(t, str) and CLIMATE_RE.search(t):
                out.append((attr, t))
    except Exception:
        pass
    if not out:
        try:
            t = filing.text() if hasattr(filing, "text") else str(filing)
            if t and CLIMATE_RE.search(t):
                out.append(("fulltext", t))
        except Exception:
            pass
    return out


def process_company(con, ticker, year=0, max_sections=4):
    co = Company(ticker)
    filings = co.get_filings(form=["10-K", "20-F"])
    if filings is None or len(filings) == 0:
        return 0
    f = filings.latest()
    if f is None:
        return 0
    accession = getattr(f, "accession_no", None) or str(f)
    url = getattr(f, "url", None) or ""
    fd = getattr(f, "filing_date", None)
    fyear = year or (fd.year - 1 if hasattr(fd, "year")
                     else int(str(fd)[:4]) - 1 if fd else 0)
    cid = con.execute("SELECT id FROM companies WHERE ticker=?",
                      (ticker.upper(),)).fetchone()
    cid = cid["id"] if cid else None
    con.execute(
        """INSERT OR IGNORE INTO reports
           (company_id, year, kind, url, acquisition_method)
           VALUES(?,?,?,?,?)""",
        (cid, fyear, "10-K", url, "api"))
    rid = con.execute(
        "SELECT id FROM reports WHERE company_id IS ? AND year IS ? AND url=?",
        (cid, fyear, url)).fetchone()
    rid = rid["id"] if rid else None
    n = 0
    for name, text in sections_with_climate(f)[:max_sections]:
        for it in regex_extract(text):
            if rid:
                con.execute(
                    """INSERT INTO extractions
                       (report_id, metric_key, value, unit, year_covered, scope,
                        target_year, page, snippet, confidence, tier, model)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rid, it["metric"], it["value"], it["unit"],
                     it.get("year"), it.get("scope"), it.get("target_year"),
                     None, it["snippet"], it["confidence"], "regex",
                     "edgartools"))
                n += 1
    con.commit()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args()
    if not HAVE_EDGAR:
        print("edgartools not installed — pip install edgartools")
        return
    _identity()
    con = db()
    rows = con.execute(
        """SELECT ticker FROM companies WHERE ticker IS NOT NULL AND ticker!=''
           ORDER BY priority DESC LIMIT ?""", (a.limit,)).fetchall()
    total = 0
    for r in rows:
        try:
            n = process_company(con, r["ticker"])
            total += n
            if n:
                print(f"{r['ticker']}: {n} filed-claim extractions")
        except Exception as e:
            print(f"{r['ticker']}: {e}")
    print(f"filings: {total} extractions from 10-K/20-F")
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('filings',?,?,?,?)",
        (now(), "", 1, total))
    con.commit()


if __name__ == "__main__":
    main()
