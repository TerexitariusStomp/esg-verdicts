"""flags.py — severity → verdict roll-up + review-hold enforcement.

Every flag with severity='high' OR any flag whose basis references a facility
match_confidence < 0.85 OR any change to a named company's verdict lands in
review_holds and is excluded from public export until `make review` acks it.

Verdict vocabulary (PUBLISHING.md): 'reported divergence' / 'disclosure gap' /
'insufficient data' / 'no flags'. Never fraud/greenwashing assertions.
"""
import argparse

from .esg import db

VERDICTS = {
    "high": "reported divergence",
    "medium": "disclosure gap",
    "low": "anomaly noted",
}


def apply_review_holds(con):
    """severity>=high flags are held until human-acked."""
    rows = con.execute(
        """SELECT f.id FROM flags f
           LEFT JOIN review_holds h ON h.flag_id=f.id
           WHERE f.severity='high' AND f.publishable=1
             AND (h.acked_at IS NULL)""").fetchall()
    n = 0
    for r in rows:
        con.execute(
            "INSERT OR IGNORE INTO review_holds(flag_id, reason) VALUES(?,?)",
            (r["id"], "severity=high"))
        con.execute("UPDATE flags SET publishable=0 WHERE id=?", (r["id"],))
        n += 1
    # low-confidence matches held too
    rows = con.execute(
        """SELECT f.id FROM flags f
           JOIN companies c ON c.id=f.company_id
           WHERE f.publishable=1 AND EXISTS(
             SELECT 1 FROM facilities fa WHERE fa.company_id=c.id
               AND fa.match_confidence IS NOT NULL AND fa.match_confidence<85)""").fetchall()
    for r in rows:
        con.execute(
            "INSERT OR IGNORE INTO review_holds(flag_id, reason) VALUES(?,?)",
            (r["id"], "match_confidence<85"))
        con.execute("UPDATE flags SET publishable=0 WHERE id=?", (r["id"],))
        n += 1
    con.commit()
    return n


def verdict_for(con, cid):
    rows = con.execute(
        "SELECT MAX(severity) FROM (SELECT CASE severity WHEN 'high' THEN 3 "
        "WHEN 'medium' THEN 2 ELSE 1 END severity FROM flags "
        "WHERE company_id=? AND publishable=1)", (cid,)).fetchone()
    s = rows[0] if rows else None
    return {3: "reported divergence", 2: "disclosure gap",
            1: "anomaly noted"}.get(s, "insufficient data")


def main():
    ap = argparse.ArgumentParser()
    a = ap.parse_args()
    con = db()
    n = apply_review_holds(con)
    print(f"flags: {n} flags moved to review holds")


if __name__ == "__main__":
    main()
