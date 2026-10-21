#!/usr/bin/env python3
"""review CLI — human ack queue for held flags (defamation + false-positive
control). `make review` lists holds; ack one to release it to public export.

Usage:
  python tools/review/cli.py            # list holds
  python tools/review/cli.py --ack FLAG_ID [--by NAME]   # release to export
  python tools/review/cli.py --show FLAG_ID              # evidence side-by-side
"""
import argparse
import os
import sqlite3
import sys

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "esg.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ack", type=int, default=0)
    ap.add_argument("--show", type=int, default=0)
    ap.add_argument("--by", default="")
    a = ap.parse_args()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    if a.show:
        r = con.execute(
            """SELECT h.*, f.flag, f.severity, f.posture, f.basis, f.pin,
                      f.year, c.name FROM review_holds h
               JOIN flags f ON f.id=h.flag_id JOIN companies c ON c.id=f.company_id
               WHERE h.flag_id=?""", (a.show,)).fetchone()
        if not r:
            print("no hold", a.show); return
        print(f"FLAG {r['flag_id']} — {r['name']}\n"
              f"  flag: {r['flag']} ({r['severity']}, {r['posture']})\n"
              f"  basis: {r['basis']}\n  pin: {r['pin']}  year: {r['year']}\n"
              f"  held: {r['held_at']}  reason: {r['reason']}")
        return
    if a.ack:
        con.execute(
            "UPDATE review_holds SET acked_at=datetime('now'), acked_by=? "
            "WHERE flag_id=?", (a.by or "human", a.ack))
        con.execute("UPDATE flags SET publishable=1 WHERE id=?", (a.ack,))
        con.commit()
        print(f"ack FL-{a.ack:05d} — released to export")
        return
    rows = con.execute(
        """SELECT h.flag_id, h.reason, h.held_at, f.flag, f.severity, c.name
           FROM review_holds h JOIN flags f ON f.id=h.flag_id
           JOIN companies c ON c.id=f.company_id
           WHERE h.acked_at IS NULL ORDER BY h.held_at""").fetchall()
    print(f"{len(rows)} holds:")
    for r in rows:
        print(f"  FL-{r['flag_id']:05d} [{r['severity']:6}] {r['name']}: "
              f"{r['flag']} ({r['reason']})")
    if rows:
        print("\nack one: make review → tools/review/cli.py --ack <flag_id>")


if __name__ == "__main__":
    main()
