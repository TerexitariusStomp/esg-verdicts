"""universe.py — seed the companies table from SEC EDGAR + GLEIF.

edgartools does the heavy lifting (tickers, CIK, exchanges, names).
GLEIF (CC0) resolves LEI + parent trees for geo/entity matching.
Resumable: unique(name, ticker) upserts; --limit for slices.
"""
import argparse
import json
import os
import time
import urllib.request

from .esg import db, now

EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers.json"
GLEIF_SEARCH = "https://api.gleif.org/api/v1/lei-lookup/autocomplete?field=fulltext&q={}"
UA = {"User-Agent": "esg-verdicts research (contact: ops@esg-verdicts.example)"}


def fetch_json(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def seed_edgar(con, cache="data/edgar_tickers.json"):
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(cache):
        data = fetch_json(EDGAR_TICKERS)
        with open(cache, "wb") as f:
            f.write(json.dumps(data).encode())
    data = json.load(open(cache))
    rows = [(v["title"], str(v["ticker"]), str(v["cik_str"]))
            for v in data.values() if v.get("title")]
    con.executemany(
        "INSERT OR IGNORE INTO companies(name,ticker,cik) VALUES(?,?,?)", rows)
    con.commit()
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('sec_edgar',?,?,?,?)",
        (now(), str(len(rows)), 7, len(rows)))
    con.commit()
    return len(rows)


def resolve_gleif(con, limit=0, sleep=0.15):
    """Best-effort LEI lookup by company name — fills lei + country.
    Not exhaustive; GLEIF autocomplete is fuzzy but good enough to
    join parent trees later via /lei-records."""
    rows = con.execute(
        "SELECT id,name FROM companies WHERE lei IS NULL ORDER BY priority DESC"
        + (f" LIMIT {limit}" if limit else "")).fetchall()
    n = 0
    for r in rows:
        try:
            res = fetch_json(GLEIF_SEARCH.format(
                urllib.request.quote(r["name"])), timeout=15)
            attrs = (res.get("data") or [{}])[0].get("attributes") or {}
            lei = attrs.get("lei")
            entity = attrs.get("entity") or {}
            country = (entity.get("legalAddress") or {}).get("country")
            if lei:
                con.execute(
                    "UPDATE companies SET lei=?, country=COALESCE(country,?) WHERE id=?",
                    (lei, country, r["id"]))
                n += 1
        except Exception:
            pass
        time.sleep(sleep)
        if n % 200 == 0:
            con.commit()
    con.commit()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gleif", action="store_true", help="resolve LEI too")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    con = db()
    n = seed_edgar(con)
    print(f"universe: {n} companies seeded from EDGAR")
    if a.gleif:
        m = resolve_gleif(con, a.limit or 0)
        print(f"universe: {m} LEI resolved")


if __name__ == "__main__":
    main()
