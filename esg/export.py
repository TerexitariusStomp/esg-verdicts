"""export.py — generate the canonical public layer:
  registry/*.csv (schema'd), figures.yaml values, parquet artifacts,
  web/index.json + per-company JSON.

License gating: verification_only sources never leave data/.
Review gating: publishable=0 flags/verdicts never appear in exports.
"""
import argparse
import csv
import json
import os

import yaml

from .esg import db, now, sha256_file
from .flags import verdict_for

REG = "registry"
WEB_DATA = "web/data"


def export_exhibits(con):
    """Every sha256'd source artifact -> exhibits.csv rows (EX-###)."""
    rows = con.execute(
        """SELECT id, company_id, url, sha256, path, acquired_at,
                  acquisition_method FROM reports WHERE sha256 IS NOT NULL
           ORDER BY id""").fetchall()
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            "exhibit_id": f"EX-{i:04d}", "kind": "report_pdf",
            "company_id": r["company_id"], "sha256": r["sha256"],
            "source_url": r["url"], "path": r["path"],
            "acquired_at": r["acquired_at"] or "",
            "acquisition_method": r["acquisition_method"] or "http_get",
            "posture": "documented"})
    _w(f"{REG}/exhibits.csv", out)


def export_statements(con):
    """Filing assertions + video claims -> statements.csv."""
    out = []
    # filing-derived: extractions w/ verbatim snippet
    for r in con.execute(
        """SELECT e.id, e.metric_key, e.value, e.unit, e.year_covered,
                  e.snippet, r.company_id, r.id rid, e.page
           FROM extractions e JOIN reports r ON r.id=e.report_id
           WHERE e.snippet IS NOT NULL ORDER BY e.id""").fetchall():
        out.append({
            "statement_id": f"ST-E{r['id']:06d}", "company_id": r["company_id"],
            "speaker_role": "official", "speaker": "",
            "auth_status": "platform_render", "series": "R",
            "pin": f"report#{r['rid']}@p{r['page']}",
            "metric_key": r["metric_key"], "value": r["value"],
            "unit": r["unit"], "snippet": r["snippet"],
            "date": str(r["year_covered"] or ""), "exhibit_id": ""})
    # video claims
    for r in con.execute(
            "SELECT * FROM claims ORDER BY id").fetchall():
        out.append({
            "statement_id": f"ST-C{r['id']:06d}", "company_id": r["company_id"],
            "speaker_role": "party_principal", "speaker": r["speaker"] or "",
            "auth_status": "quoted", "series": "V",
            "pin": f"yt#{r['video_id']}@{int(r['start_s'])}",
            "metric_key": r["metric_key"], "value": r["value"],
            "unit": r["unit"], "snippet": r["snippet"] or "",
            "date": r["date"] or "", "exhibit_id": ""})
    _w(f"{REG}/statements.csv", out)


def export_flags(con):
    out = []
    for r in con.execute(
            "SELECT * FROM flags WHERE publishable=1 "
            "ORDER BY company_id, severity DESC").fetchall():
        out.append({
            "flag_id": f"FL-{r['id']:05d}", "company_id": r["company_id"],
            "flag": r["flag"], "severity": r["severity"],
            "posture": r["posture"], "basis": r["basis"],
            "pin": r["pin"] or "", "year": r["year"] or ""})
    _w(f"{REG}/flags.csv", out)


def export_figures(con):
    figs = yaml.safe_load(open(f"{REG}/figures.yaml"))["figures"]
    for k, f in figs.items():
        try:
            f["value"] = con.execute(f["derivation"]).fetchone()[0] or 0
        except Exception:
            f["value"] = 0
    yaml.safe_dump({"figures": figs}, open(f"{REG}/figures.yaml", "w"),
                   sort_keys=False)


def export_stats(con):
    """Coverage metrics — the public marketing surface for the registry."""
    q = lambda s: con.execute(s).fetchone()[0]
    return {
        "companies_total": q("SELECT COUNT(*) FROM companies"),
        "companies_with_reports": q(
            "SELECT COUNT(DISTINCT company_id) FROM reports"),
        "companies_with_pdf": q(
            "SELECT COUNT(DISTINCT company_id) FROM reports "
            "WHERE path IS NOT NULL"),
        "companies_with_extractions": q(
            "SELECT COUNT(DISTINCT r.company_id) FROM extractions e "
            "JOIN reports r ON r.id=e.report_id"),
        "reports_cataloged": q("SELECT COUNT(*) FROM reports"),
        "reports_harvested": q(
            "SELECT COUNT(*) FROM reports WHERE path IS NOT NULL"),
        "verified_metrics": q("SELECT COUNT(*) FROM extractions"),
        "claims": q("SELECT COUNT(*) FROM claims"),
        "flags_publishable": q(
            "SELECT COUNT(*) FROM flags WHERE publishable=1"),
        "flags_documented": q(
            "SELECT COUNT(*) FROM flags WHERE publishable=1 "
            "AND posture='documented'"),
        "facilities": q("SELECT COUNT(*) FROM facilities"),
        "litigation_matters": q("SELECT COUNT(*) FROM litigation"),
    }


def export_web(con):
    os.makedirs(WEB_DATA, exist_ok=True)
    companies = []
    for c in con.execute(
            "SELECT id, name, ticker, country, sector, priority FROM companies "
            "ORDER BY priority DESC, name").fetchall():
        fl = con.execute(
            "SELECT COUNT(*) n FROM flags WHERE company_id=? AND publishable=1",
            (c["id"],)).fetchone()["n"]
        ex = con.execute(
            "SELECT COUNT(*) n FROM extractions e JOIN reports r "
            "ON r.id=e.report_id WHERE r.company_id=?", (c["id"],)).fetchone()["n"]
        fc = con.execute(
            "SELECT COUNT(*) n FROM facilities WHERE company_id=?",
            (c["id"],)).fetchone()["n"]
        rp = con.execute(
            "SELECT COUNT(*) n FROM reports WHERE company_id=? "
            "AND path IS NOT NULL", (c["id"],)).fetchone()["n"]
        companies.append({
            "id": c["id"], "name": c["name"], "ticker": c["ticker"],
            "country": c["country"], "sector": c["sector"],
            "verdict": verdict_for(con, c["id"]), "flags": fl,
            "reports": rp, "extractions": ex, "facilities": fc})
    with open(f"{WEB_DATA}/index.json", "w") as f:
        json.dump({"generated": now(), "stats": export_stats(con),
                   "companies": companies}, f)
    # per-company detail for the UI drawer
    os.makedirs(f"{WEB_DATA}/company", exist_ok=True)
    for c in companies[:200]:
        cid = c["id"]
        detail = dict(c)
        detail["flags"] = [dict(r) for r in con.execute(
            "SELECT flag,severity,posture,basis,pin,year FROM flags "
            "WHERE company_id=? AND publishable=1", (cid,)).fetchall()]
        detail["extractions"] = [dict(r) for r in con.execute(
            """SELECT e.metric_key,e.value,e.unit,e.year_covered,e.page,
                      e.snippet,e.tier,r.year,r.url
               FROM extractions e JOIN reports r ON r.id=e.report_id
               WHERE r.company_id=? LIMIT 200""", (cid,)).fetchall()]
        detail["facilities"] = [dict(r) for r in con.execute(
            "SELECT name,lat,lon,emissions,sector,year,match_confidence "
            "FROM facilities WHERE company_id=? LIMIT 500", (cid,)).fetchall()]
        detail["claims"] = [dict(r) for r in con.execute(
            "SELECT video_id,start_s,speaker,claim_type,metric_key,value,"
            "unit,snippet,link FROM claims WHERE company_id=?",
            (cid,)).fetchall()]
        with open(f"{WEB_DATA}/company/{cid}.json", "w") as f:
            json.dump(detail, f)
    return len(companies)


def export_parquet(con):
    try:
        import pandas as pd
        os.makedirs("web/data/parquet", exist_ok=True)
        for tbl in ("companies", "reports", "extractions", "facilities",
                    "verification", "claims", "flags", "litigation"):
            df = pd.read_sql_query(f"SELECT * FROM {tbl}", con)
            for col in df.columns:
                if df[col].dtype == object:
                    df[col] = df[col].astype(str)
            df.to_parquet(f"web/data/parquet/{tbl}.parquet")
        return True
    except Exception as e:
        print(f"parquet: {e}")
        return False


def _w(path, rows):
    if not rows:
        open(path, "w").write("")
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    a = ap.parse_args()
    con = db()
    export_exhibits(con)
    export_statements(con)
    export_flags(con)
    export_figures(con)
    n = export_web(con)
    ok = export_parquet(con)
    # bump export version for custody meta
    v = int(open("data/.export_version").read() or 0) \
        if os.path.exists("data/.export_version") else 0
    os.makedirs("data", exist_ok=True)
    with open("data/.export_version", "w") as f:
        f.write(str(v + 1))
    print(f"export: registry + web data for {n} companies; parquet={ok}")


if __name__ == "__main__":
    main()
