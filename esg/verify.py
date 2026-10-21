"""verify.py — divergence computation: filed claims vs physical ground truth.

For every company with both (a) a filed Scope-1 extraction and (b) facilities
with Climate TRACE / GHGRP emissions, compute divergence_pct and write a
`verification` row + a `flag` when thresholds trip. Also emits disclosure-gap
and YoY-anomaly flags.

Posture: divergence is a documented observation — never a fraud assertion.
"""
import argparse

from .esg import db, now

DIVERGENCE_FLAG_PCT = 30.0   # |filed - truth| / truth > 30% -> flag
GAP_FACILITY_EMISSIONS = 0   # any facilities + no scope1 -> gap candidate
YOY_JUMP_PCT = 50.0          # unexplained >50% YoY swing


def filed_scope1(con, cid, year=None):
    q = """SELECT e.value, e.year_covered, r.year, e.snippet, r.id rid, e.page
           FROM extractions e JOIN reports r ON r.id=e.report_id
           WHERE r.company_id=? AND e.metric_key='scope1_mtco2e'"""
    if year:
        q += f" AND (e.year_covered={year} OR r.year={year})"
    return con.execute(q, (cid,)).fetchall()


def facility_sum(con, cid, year=None):
    q = ("SELECT SUM(emissions) s, COUNT(*) n FROM facilities "
         "WHERE company_id=? AND emissions IS NOT NULL")
    if year:
        q += f" AND year={year}"
    r = con.execute(q, (cid,)).fetchone()
    return (r["s"], r["n"]) if r else (0, 0)


def flag(con, cid, flag_name, severity, posture, basis, pin=None, year=None):
    con.execute(
        """INSERT OR IGNORE INTO flags
           (company_id, flag, severity, posture, basis, pin, year, publishable)
           VALUES(?,?,?,?,?,?,?,1)""",
        (cid, flag_name, severity, posture, basis, pin, year))


def verify_company(con, cid):
    made = 0
    years = {r["year_covered"] or r["year"] for r in filed_scope1(con, cid)}
    years |= {y for (y,) in con.execute(
        "SELECT DISTINCT year FROM facilities WHERE company_id=?", (cid,))}
    for y in [y for y in years if y]:
        filed = filed_scope1(con, cid, y)
        fs, nf = facility_sum(con, cid, y)
        if filed and fs and nf:
            best = max(filed, key=lambda r: r["value"] or 0)
            div = (best["value"] - fs) / fs * 100.0 if fs else 0
            con.execute(
                """INSERT OR REPLACE INTO verification
                   (company_id, metric, claimed, ground_truth, source,
                    divergence_pct, flag, year)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (cid, "scope1_mtco2e", best["value"], fs, "climatetrace",
                 round(div, 1),
                 "over" if div > DIVERGENCE_FLAG_PCT
                 else "under" if div < -DIVERGENCE_FLAG_PCT else "in_range", y))
            if abs(div) > DIVERGENCE_FLAG_PCT:
                sev = "high" if abs(div) > 100 else "medium"
                flag(con, cid, "reported_divergence", sev, "documented",
                     f"filed scope1 {best['value']:,.0f} t vs Σfacility "
                     f"{fs:,.0f} t ({div:+.0f}%)", f"report#{best['rid']}",
                     y)
                made += 1
        elif fs and nf and not filed:
            flag(con, cid, "disclosure_gap", "medium", "reported",
                 f"{nf} facilities w/ Σ{fs:,.0f} t ground-truth emissions "
                 f"but no filed scope-1 disclosure", None, y)
            made += 1
    # YoY anomaly on filed scope1
    rows = sorted(filed_scope1(con, cid),
                  key=lambda r: r["year_covered"] or r["year"] or 0)
    prev = None
    for r in rows:
        if prev and prev["value"] and r["value"]:
            jump = abs(r["value"] - prev["value"]) / prev["value"] * 100
            if jump > YOY_JUMP_PCT:
                flag(con, cid, "yoy_anomaly", "low", "documented",
                     f"filed scope1 {prev['value']:,.0f} → {r['value']:,.0f} t "
                     f"({jump:+.0f}%)", f"report#{r['rid']}",
                     r["year_covered"] or r["year"])
                made += 1
        prev = r
    con.commit()
    return made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    con = db()
    q = ("SELECT DISTINCT company_id FROM facilities WHERE company_id IS NOT NULL"
         " UNION SELECT DISTINCT company_id FROM reports WHERE company_id IS NOT NULL")
    if a.limit:
        q = f"SELECT DISTINCT company_id FROM ({q}) LIMIT {a.limit}"
    total = 0
    for r in con.execute(q).fetchall():
        total += verify_company(con, r["company_id"])
    print(f"verify: {total} flags produced")
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('verify',?,?,?,?)",
        (now(), "", 1, total))
    con.commit()


if __name__ == "__main__":
    main()
