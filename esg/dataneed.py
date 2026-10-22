"""dataneed.py — ingest the DataNeed/company-reports HF dataset as a
discovery catalog. ~350k rows carry company_name + company_website + year +
document_category + a DIRECT ISSUER-HOSTED PDF URL — fetching spreads
across thousands of independent domains, so there is no shared rate-limit
chokepoint (the RR/IA/tor bottleneck disappears for matched reports).

We use the dataset ONLY for discovery (names/URLs/years are facts); the
PDFs are fetched from the issuers themselves via the --issuer harvest
lane with full provenance (acquisition_method='issuer_direct').

Modes:
  python -m esg.dataneed download   # 5 JSON parts (~2GB) -> data/source/
  python -m esg.dataneed ingest     # parse -> companies + reports rows
"""
import argparse
import json
import os
import re
import urllib.request

from .catalog import _FIN, _HEAVY, _norm
from .esg import db, now

BASE = ("https://huggingface.co/datasets/DataNeed/"
        "company-reports/resolve/main/data/")
FILES = ["company_reports_2024_01_19.json",
         "company_reports_2024_01_21.json",
         "company_reports_2024_01_22.json",
         "company_reports_2024_01_23.json",
         "company_reports_2024_02_03.json"]
DEST = "data/source/dataneed"

_KIND_MAP = (
    (re.compile(r"sustainab|esg|csr|responsib|impact|tcfd|climate|"
                r"environment", re.I), "sustainability_report"),
    (re.compile(r"annual|10-k|10k|integrated|financial", re.I),
     "annual_report"),
)


def _kind(category):
    for pat, k in _KIND_MAP:
        if pat.search(category or ""):
            return k
    return "other_report"


def download():
    os.makedirs(DEST, exist_ok=True)
    for fn in FILES:
        path = f"{DEST}/{fn}"
        if os.path.exists(path) and os.path.getsize(path) > 50_000_000:
            print(f"dataneed: {fn} already downloaded "
                  f"({os.path.getsize(path) // 1048576}MB)", flush=True)
            continue
        print(f"dataneed: downloading {fn} ...", flush=True)
        req = urllib.request.Request(
            BASE + fn,
            headers={"User-Agent": "esg-verdicts/1.0 research crawler"})
        with urllib.request.urlopen(req, timeout=120) as r, \
                open(path, "wb") as f:
            n = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                n += len(chunk)
        print(f"dataneed: {fn} -> {n // 1048576}MB", flush=True)


def _records(path):
    """Stream top-level JSON-array records from a (large) file without
    materializing it — incremental buffer + raw_decode."""
    dec = json.JSONDecoder()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        buf = ""
        started = False
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            buf += chunk
            while True:
                if not started:
                    i = buf.find("[")
                    if i < 0:
                        buf = buf[-4096:]
                        break
                    buf = buf[i + 1:]
                    started = True
                buf = buf.lstrip(" \n\t,")
                if not buf:
                    break
                try:
                    obj, j = dec.raw_decode(buf)
                except json.JSONDecodeError:
                    break
                yield obj
                buf = buf[j:]


def _company_map(con):
    """name_norm -> company_id for every existing company (first wins on
    collisions — fuzzy merges are a later refinement, not an ingest-blocker)."""
    m = {}
    for cid, name in con.execute("SELECT id, name FROM companies"):
        m.setdefault(_norm(name or ""), cid)
    return m


def _match(nn, cmap, names):
    """Display-name ('Yara') vs legal-name ('Yara International ASA') match:
    exact norm -> prefix containment -> rapidfuzz (same threshold as the
    catalog index matcher)."""
    from rapidfuzz import fuzz
    if not nn:
        return None
    if nn in cmap:
        return cmap[nn]
    for k, cid in cmap.items():
        if len(nn) >= 4 and (k.startswith(nn) or nn.startswith(k)):
            return cid
    best = max(names, key=lambda kv: fuzz.ratio(_norm(kv[1]), nn),
               default=None)
    if best and fuzz.ratio(_norm(best[1]), nn) >= 88:
        return best[0]
    return None


def ingest():
    con = db()
    con.execute("PRAGMA busy_timeout=120000")
    cmap = _company_map(con)
    names = con.execute("SELECT id, name FROM companies").fetchall()
    seen_stub = set()   # names already stubbed this run
    stats = dict(records=0, pdfs=0, matched=0, stubs=0, rows=0)
    for fn in FILES:
        path = f"{DEST}/{fn}"
        if not os.path.exists(path):
            print(f"dataneed: {fn} missing — run download first", flush=True)
            continue
        for rec in _records(path):
            stats["records"] += 1
            url = (rec.get("url") or "").strip()
            if ".pdf" not in url.lower():
                continue
            stats["pdfs"] += 1
            name = (rec.get("company_name") or "").strip()
            nn = _norm(name)
            cid = cmap.get(nn) or _match(nn, cmap, names)
            if cid is None:
                if not nn or nn in seen_stub:
                    continue
                seen_stub.add(nn)
                pri = 300 if _HEAVY.search(name) else (
                    100 if _FIN.search(name) else 0)
                cur = con.execute(
                    "INSERT INTO companies (name, priority) VALUES(?,?)",
                    (name or nn, pri))
                cid = cur.lastrowid
                cmap[nn] = cid
                stats["stubs"] += 1
            try:
                year = int(str(rec.get("year") or "").strip()[:4])
                if not 1990 <= year <= 2030:
                    continue
            except ValueError:
                continue
            cur = con.execute(
                """INSERT OR IGNORE INTO reports
                   (company_id, year, kind, url, acquisition_method)
                   VALUES(?,?,?,?,?)""",
                (cid, year, _kind(rec.get("document_category")),
                 url, "dataneed"))
            stats["rows"] += cur.rowcount
            if stats["pdfs"] % 10000 == 0:
                con.commit()
                print(f"dataneed: {stats}", flush=True)
        con.commit()
        print(f"dataneed: {fn} done — {stats}", flush=True)
    # marker job: supervisor treats ingest as one-shot
    con.execute(
        "INSERT OR REPLACE INTO jobs(kind,status,payload) VALUES(?,?,?)",
        ("dataneed", "done", json.dumps(stats)))
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots "
        "VALUES('dataneed',?,?,?,?)",
        (now(), "", stats["rows"], stats["records"]))
    con.commit()
    print(f"dataneed ingest: {stats}", flush=True)
    return stats


def rematch():
    """Repoint reports from DataNeed-created stub companies to the real
    company rows they match (first-ingest ran with exact-norm matching and
    stubbed everything). Only touches stubs (no ticker/cik/lei) that own
    dataneed-sourced reports; target must predate the stub."""
    con = db()
    con.execute("PRAGMA busy_timeout=120000")
    stubs = con.execute(
        """SELECT DISTINCT c.id, c.name FROM companies c
           WHERE c.ticker IS NULL AND c.cik IS NULL AND c.lei IS NULL
             AND EXISTS (SELECT 1 FROM reports r WHERE r.company_id=c.id
                         AND r.acquisition_method='dataneed')""").fetchall()
    names = con.execute(
        "SELECT id, name FROM companies WHERE ticker IS NOT NULL"
        " OR cik IS NOT NULL OR lei IS NOT NULL").fetchall()
    cmap = _company_map(con)
    moved = 0
    for cid, cname in stubs:
        target = _match(_norm(cname), {k: v for k, v in cmap.items()
                                     if v != cid}, names)
        if not target or target == cid:
            continue
        # repoint what fits; rows colliding on (company_id,year,url) are
        # same-report duplicates the target already holds — drop them
        for rid, in con.execute(
                "SELECT id FROM reports WHERE company_id=?",
                (cid,)).fetchall():
            try:
                con.execute(
                    "UPDATE reports SET company_id=? WHERE id=?",
                    (target, rid))
            except Exception:
                con.execute("DELETE FROM reports WHERE id=?", (rid,))
        con.execute("DELETE FROM companies WHERE id=?", (cid,))
        moved += 1
    con.commit()
    print(f"rematch: {moved}/{len(stubs)} stub companies merged into "
          "existing companies", flush=True)
    return moved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["download", "ingest", "rematch"])
    a = ap.parse_args()
    if a.cmd == "download":
        download()
    elif a.cmd == "rematch":
        rematch()
    else:
        ingest()


if __name__ == "__main__":
    main()
