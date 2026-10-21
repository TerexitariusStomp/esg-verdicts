"""geo.py — facility ground truth via Climate TRACE v7 (+ EPA GHGRP importer).

Owner-first resolution (better than v1's bulk-ingest-then-match):
  1. /v7/owners?name=<company> -> owner ids + RapidFuzz match_confidence
  2. /v7/sources?ownerIds=<ids>&year=YYYY -> facilities land pre-attributed
This means the Σfacility ground truth exists only for matched companies —
which is exactly the set verification needs.

EPA GHGRP: flat-file importer (ghgdata.epa.gov) — optional; env ESG_GHGRP_CSV.
"""
import argparse
import json
import os
import time
import urllib.request

from rapidfuzz import fuzz

from .esg import db, now

CT = "https://api.climatetrace.org/v7"
UA = {"User-Agent": "esg-verdicts/1.0 research"}


def ct(path, timeout=60):
    req = urllib.request.Request(CT + path, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def find_owners(company_name):
    """Try the raw name, then progressively simpler variants — SEC names like
    'ExxonMobil Holdings Corp' often don't match CT's 'Exxon Mobil Corp'."""
    import re
    variants = [company_name]
    cleaned = re.sub(r"\b(holdings?|holding|group|incorporated|inc|"
                     r"corporation|corp|company|co|limited|ltd|plc|llc)\b\.?",
                     "", company_name, flags=re.I).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    if cleaned and cleaned != company_name:
        variants.append(cleaned)
    # split camel-case / spaced variants of the core token
    core = cleaned.split()[0] if cleaned.split() else company_name
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", core)
    if spaced != core:
        variants.append(spaced)
    seen, out = set(), []
    for v in variants:
        if v in seen:
            continue
        seen.add(v)
        try:
            res = ct("/owners?name=" + urllib.request.quote(v))
        except Exception:
            continue
        if isinstance(res, list) and res:
            out = res
            break
    return out


def company_sources(owner_ids, year):
    ids = ",".join(owner_ids)
    try:
        return ct(f"/sources?year={year}&ownerIds={ids}&limit=10000")
    except Exception:
        return []


def ingest_company(con, c, year=2023, thresh=80.0, sleep=0.2):
    """Resolve owners -> ingest their facilities with company_id attached."""
    owners = find_owners(c["name"])
    best = None
    ids = []
    for o in owners:
        score = fuzz.token_set_ratio(_norm(o.get("name", "")),
                                     _norm(c["name"])) / 1.0
        if score >= thresh and (best is None or score > best):
            best = score
        if score >= thresh:
            ids.append(o["id"])
    if not ids and owners:
        # fall back to the single best candidate above a lower bar
        cand = max(owners, key=lambda o: fuzz.token_set_ratio(
            _norm(o["name"]), _norm(c["name"])))
        score = fuzz.token_set_ratio(_norm(cand["name"]), _norm(c["name"]))
        if score >= 65:
            ids = [cand["id"]]
            best = score
    if not ids:
        return 0, best
    n = 0
    for s in company_sources(ids, year):
        cen = s.get("centroid") or {}
        con.execute(
            """INSERT OR IGNORE INTO facilities
               (company_id, name, owner, lat, lon, country, sector, source,
                external_id, emissions, capacity, status, year,
                match_confidence)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c["id"], s.get("name"), ",".join(
                o.get("name", "") for o in owners if o.get("id") in ids),
             cen.get("latitude"), cen.get("longitude"), s.get("country"),
             s.get("sector"), "climatetrace", str(s.get("id")),
             s.get("emissionsQuantity"), s.get("capacity"), "active",
             s.get("year", year), round(best or 0, 1)))
        n += 1
    con.commit()
    return n, best


def _norm(s):
    import re
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def refresh_emissions_rank(con):
    """Emissions rank: rank companies by summed facility emissions desc
    (rank 1 = biggest emitter) -> priority() input."""
    con.execute("UPDATE companies SET emissions_rank=NULL")
    rows = con.execute(
        """SELECT company_id, SUM(emissions) e FROM facilities
           WHERE company_id IS NOT NULL AND emissions IS NOT NULL
           GROUP BY company_id ORDER BY e DESC""").fetchall()
    for i, r in enumerate(rows):
        con.execute("UPDATE companies SET emissions_rank=? WHERE id=?",
                    (i + 1, r["company_id"]))
    con.commit()
    return len(rows)


def import_ghgrp(con, csv_path):
    """Optional EPA GHGRP flat-file importer. Expected columns vary by release;
    map the stable ones. Ground-truth tier: facilities are self-reported to EPA."""
    import csv
    n = 0
    with open(csv_path, newline="", errors="ignore") as f:
        for row in csv.DictReader(f):
            name = (row.get("Facility Name") or row.get("FACILITY NAME")
                    or row.get("facility_name") or "").strip()
            if not name:
                continue
            lat = _f(row.get("Latitude") or row.get("LATITUDE"))
            lon = _f(row.get("Longitude") or row.get("LONGITUDE"))
            em = _f(row.get("Total reported direct emissions")
                    or row.get("GHG QUANTITY") or row.get("total_reported"))
            con.execute(
                """INSERT OR IGNORE INTO facilities
                   (company_id, name, owner, lat, lon, country, sector, source,
                    external_id, emissions, capacity, status, year,
                    match_confidence)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (None, name, row.get("Parent Company") or row.get("PARENT COMPANY"),
                 lat, lon, "USA", row.get("Industry Type") or "", "epa_ghgrp",
                 str(row.get("Facility Id") or row.get("FACILITY ID") or n),
                 em, None, "reported", _f(row.get("Reporting Year") or ""), None))
            n += 1
    con.commit()
    return n


def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--year", type=int, default=2023)
    ap.add_argument("--ghgrp", default="",
                    help="path to EPA GHGRP flat CSV to import")
    a = ap.parse_args()
    con = db()
    if a.ghgrp:
        n = import_ghgrp(con, a.ghgrp)
        print(f"ghgrp: {n} facilities imported")
    q = ("SELECT id, name FROM companies ORDER BY priority DESC, id")
    if a.limit:
        q += f" LIMIT {a.limit}"
    total = 0
    for c in con.execute(q).fetchall():
        try:
            n, conf = ingest_company(con, c, a.year)
            total += n
            if n:
                print(f"{c['name']}: {n} facilities (conf {conf})")
        except Exception as e:
            print(f"{c['name']}: {e}")
        time.sleep(0.2)
    r = refresh_emissions_rank(con)
    from .esg import recompute_priority
    recompute_priority(con)
    con.commit()
    print(f"geo: {total} facilities ingested; {r} companies emissions-ranked")


if __name__ == "__main__":
    main()
