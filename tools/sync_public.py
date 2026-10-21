#!/usr/bin/env python3
"""sync_public.py — mirror publishable rows into the esg-grid D1 pro tables.

Runs after export (overnight stage or manually). Only publishable=1 flags
and company summary rows cross the boundary — the same data the public
site renders, so nothing private leaves the machine.

Usage: ESG_SYNC_D1=1 python tools/sync_public.py
Requires wrangler OAuth; reads grid/wrangler.toml for the DB name.
"""
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from esg.flags import verdict_for  # noqa: E402

DB = os.path.join(ROOT, "data/esg.db")
D1 = "esg-grid"


def esc(s):
    return str(s or "").replace("'", "''")


def main():
    if os.environ.get("ESG_SYNC_D1") != "1":
        print("sync_public: skipped (set ESG_SYNC_D1=1 to enable)")
        return
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    stmts = ["DELETE FROM pro_flags", "DELETE FROM pro_companies",
             "DELETE FROM pro_stats"]
    for r in con.execute(
            """SELECT f.*, c.name cname FROM flags f
               JOIN companies c ON c.id=f.company_id
               WHERE f.publishable=1""").fetchall():
        stmts.append(
            f"INSERT INTO pro_flags(id,company,flag,severity,posture,basis,"
            f"pin,year) VALUES({r['id']},'{esc(r['cname'])}',"
            f"'{esc(r['flag'])}','{r['severity']}','{r['posture']}',"
            f"'{esc(r['basis'])}','{esc(r['pin'])}','{r['year'] or ''}')")
    for r in con.execute(
            """SELECT c.id, c.name, c.ticker, c.country, c.sector,
                 (SELECT COUNT(*) FROM reports rp
                   WHERE rp.company_id=c.id AND rp.path IS NOT NULL) reports,
                 (SELECT COUNT(*) FROM extractions e JOIN reports rp2
                   ON rp2.id=e.report_id WHERE rp2.company_id=c.id) xtracts,
                 (SELECT COUNT(*) FROM facilities fc
                   WHERE fc.company_id=c.id) facs
               FROM companies c
               WHERE EXISTS (SELECT 1 FROM reports rp
                   WHERE rp.company_id=c.id)""").fetchall():
        stmts.append(
            f"INSERT INTO pro_companies(id,name,ticker,country,sector,"
            f"verdict,reports,extractions,facilities) VALUES({r['id']},"
            f"'{esc(r['name'])}','{esc(r['ticker'])}','{esc(r['country'])}',"
            f"'{esc(r['sector'])}','{verdict_for(con, r['id'])}',"
            f"{r['reports']},{r['xtracts']},{r['facs']})")
    for k, v in con.execute(
            """SELECT 'reports' k, COUNT(*) v FROM reports WHERE
               path IS NOT NULL UNION ALL
               SELECT 'flags', COUNT(*) FROM flags WHERE publishable=1
               UNION ALL SELECT 'companies',
               COUNT(DISTINCT company_id) FROM reports""").fetchall():
        stmts.append(f"INSERT INTO pro_stats(k,v) VALUES('{k}',{v})")

    sql = os.path.join(ROOT, "data/.sync_public.sql")
    with open(sql, "w") as f:
        f.write(";\n".join(stmts) + ";\n")
    r = subprocess.run(
        ["wrangler", "d1", "execute", D1, "--remote", "--file", sql],
        cwd=os.path.join(ROOT, "grid"), capture_output=True, text=True)
    print(r.stdout[-500:] if r.returncode == 0 else r.stderr[-500:])
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
