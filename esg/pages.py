"""pages.py — extract per-page text (PyMuPDF fast path, pdfplumber fallback),
detect ESG-candidate pages, enqueue extraction jobs.

Output: data/pages/<report_id>/<page>.txt + jobs(kind='extract').
langdetect marks non-English pages (English regex pack v1).
"""
import argparse
import os
import re

import fitz  # pymupdf

from .esg import db, now

try:
    from langdetect import detect as lang_detect
except Exception:
    lang_detect = lambda t: "en"  # noqa: E731

PAGES_DIR = "data/pages"

# Candidate-page detector: ESG vocabulary AND at least one numeric figure.
CAND = re.compile(
    r"scope\s*[123]|co2e|emissions|ghg|greenhouse|energy|mwh|gwh|renewable|"
    r"water|waste|recycl|net[\s-]?zero|carbon[\s-]?neutral|intensity|tcf|tcfd|"
    r"sbti|sasb|gri\b", re.I)


def page_texts(path):
    """Yield (page_no, text). PyMuPDF primary."""
    doc = fitz.open(path)
    for i, pg in enumerate(doc):
        yield i + 1, pg.get_text("text")


def process_report(con, rid, path):
    os.makedirs(f"{PAGES_DIR}/{rid}", exist_ok=True)
    pages = 0
    candidates = 0
    for pno, txt in page_texts(path):
        pages += 1
        if not txt.strip():
            continue
        with open(f"{PAGES_DIR}/{rid}/{pno}.txt", "w") as f:
            f.write(txt)
        if CAND.search(txt) and re.search(r"\d[\d,\.]*", txt):
            candidates += 1
            lang = "en"
            try:
                lang = lang_detect(txt[:1000])
            except Exception:
                pass
            con.execute(
                "INSERT OR IGNORE INTO jobs(kind,status,payload) VALUES(?,?,?)",
                ("extract", "pending",
                 f'{{"report_id":{rid},"page":{pno},"lang":"{lang}"}}'))
    con.execute("UPDATE reports SET pages=?, parse_tier='text' WHERE id=?",
                (pages, rid))
    # enqueue figure pass separately (runs only if figure deps present)
    con.execute(
        "INSERT OR IGNORE INTO jobs(kind,status,payload) VALUES(?,?,?)",
        ("figures", "pending", f'{{"report_id":{rid},"path":"{path}"}}'))
    con.commit()
    return pages, candidates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--report", type=int, default=0)
    a = ap.parse_args()
    con = db()
    q = ("SELECT id, path FROM reports WHERE sha256 IS NOT NULL AND "
         "(pages IS NULL OR pages=0)")
    if a.report:
        q = f"SELECT id, path FROM reports WHERE id={a.report}"
    elif a.limit:
        q += f" LIMIT {a.limit}"
    total_p = total_c = 0
    for r in con.execute(q).fetchall():
        try:
            p, c = process_report(con, r["id"], r["path"])
            total_p += p
            total_c += c
            print(f"report {r['id']}: {p} pages, {c} candidates")
        except Exception as e:
            con.execute(
                "INSERT OR REPLACE INTO jobs(kind,status,payload) VALUES(?,?,?)",
                ("pages", "failed", f'{{"report_id":{r["id"]},"err":"{e}"}}'))
            con.commit()
    print(f"pages: {total_p} processed, {total_c} candidate pages queued")
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('pages',?,?,?,?)",
        (now(), "", 1, total_p))
    con.commit()


if __name__ == "__main__":
    main()
