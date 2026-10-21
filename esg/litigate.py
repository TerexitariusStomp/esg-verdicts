"""litigate.py — litigation context: Sabin climate-litigation DB (via the
Climate Policy Radar litigation-data-mapper public exports) + CourtListener.

Feeds the `litigation` table — and bumps companies' litigation_count, which
feeds priority(): companies already being sued are the highest-value targets.
"""
import argparse
import csv
import io
import json
import os
import urllib.request

from .esg import db, now

# Climate Policy Radar publishes the Sabin-derived dataset; fall back to a
# cached local copy at data/source/litigation.csv if the remote moves.
CLDB_URLS = [
    "https://cdn.climatepolicyradar.org/navigator/litigation_cases.csv",
    "https://raw.githubusercontent.com/climatepolicyradar/litigation-data-mapper/main/data/litigation_cases.csv",
]
# CourtListener free search API — greenwashing-adjacent opinions/orders.
CL_API = "https://www.courtlistener.com/api/rest/v4/search/"
CL_TERMS = [
    '"carbon neutral"', '"net zero"', 'greenwashing',
    '"climate change" "consumer fraud"', '"Scope 1" OR "Scope 2" emissions',
]
UA = {"User-Agent": "esg-verdicts/1.0 research"}


def _norm(s):
    from .catalog import _norm as cn
    return cn(s)


def company_lookup(con):
    rows = con.execute("SELECT id,name,ticker FROM companies").fetchall()
    return [(r["id"], _norm(r["name"]), r["ticker"]) for r in rows]


def fetch_litigation_rows():
    for url in CLDB_URLS:
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", "ignore")
            os.makedirs("data/source", exist_ok=True)
            with open("data/source/litigation_raw.csv", "w") as f:
                f.write(body)
            return list(csv.DictReader(io.StringIO(body)))
        except Exception:
            continue
    if os.path.exists("data/source/litigation_raw.csv"):
        return list(csv.DictReader(open("data/source/litigation_raw.csv")))
    return []


def fetch_courtlistener():
    """CourtListener opinions matching greenwashing-adjacent terms."""
    import time
    rows = []
    for term in CL_TERMS:
        time.sleep(2.5)  # courtlistener rate limit ~5/min anonymous
        try:
            url = (CL_API + "?q=" + urllib.request.quote(term) +
                   "&type=o&order_by=dateFiled%20desc")
            req = urllib.request.Request(url, headers=UA)
            d = json.loads(urllib.request.urlopen(req, timeout=30).read())
            for r in d.get("results", []):
                rows.append({
                    "title": r.get("caseName") or "",
                    "filed": r.get("dateFiled") or "",
                    "docket": r.get("docketNumber") or r.get("absolute_url"),
                    "category": "courtlistener",
                    "case_parties": r.get("caseName") or "",
                    "snippet": (r.get("caseName") or "") + " — " +
                               (r.get("court") or ""),
                })
        except Exception as e:
            print(f"courtlistener '{term}': {e}")
    return rows


def run(con):
    rows = fetch_litigation_rows() or fetch_courtlistener()
    names = company_lookup(con)
    bynorm = {n: (cid, t) for cid, n, t in names}
    n = 0
    for row in rows:
        # Sabin/CPR rows: names vary; look for respondent/defendant columns
        for key in ("case_parties", "defendant", "respondents",
                    "entity", "companies", "name"):
            val = row.get(key) or ""
            if not val:
                continue
            import re as _re
            for part in _re.split(r";|\bv\.\s*|\bvs\.?\s*", val):
                nn = _norm(part)
                if not nn:
                    continue
                for cid, cname, tick in names:
                    if cname and len(nn) > 4 and len(cname) > 4 and \
                            (cname in nn or nn in cname):
                        case_id = row.get("id") or row.get("case_id") or \
                            row.get("docket") or row.get("title", "")[:60]
                        con.execute(
                            """INSERT OR IGNORE INTO litigation
                               (company_id, case_id, type, filed, status, snippet)
                               VALUES(?,?,?,?,?,?)""",
                            (cid, str(case_id),
                             row.get("category") or row.get("type"),
                             row.get("filed") or row.get("filing_date"),
                             row.get("status"),
                             (row.get("title") or row.get("summary") or "")[:400]))
                        n += 1
                        break
    # litigation_count rollup -> priority()
    con.execute("""
      UPDATE companies SET litigation_count = (
        SELECT COUNT(*) FROM litigation l WHERE l.company_id = companies.id)""")
    con.commit()
    return n


def main():
    ap = argparse.ArgumentParser()
    a = ap.parse_args()
    con = db()
    n = run(con)
    print(f"litigate: {n} litigation links")
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('litigate',?,?,?,?)",
        (now(), "", 30, n))
    con.commit()


if __name__ == "__main__":
    main()
