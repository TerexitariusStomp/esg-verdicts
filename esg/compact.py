"""compact.py — delete a report's raw PDF once every stage that needs the
file itself has finished with it. Turns the disk from a hard cap into a
circular buffer: fetch -> pages -> figures -> extract-pending -> unlink.

Safety contract:
  - pages.py and figures.py open the PDF with fitz; extract.py's
    pdfplumber table tier opens it too. All of these run during job
    status='pending'. pending_llm and everything downstream read page
    text and OCR crops only — no PDF needed.
  - So a report is compactable iff it has sha256 (fetched) AND no
    pending jobs of kind pages/figures/extract reference its report_id.
  - sha256 + url + acquisition_method stay in the row: provenance is
    preserved, harvest lanes filter on sha256 so nothing refetches,
    and evidence.py's manifest already tolerates a missing path.
"""
import json
import os
import sys

from .esg import db, now


def compactable(con, limit=1000):
    # pages>0 is required because pages.py gates on `pages IS NULL`, not
    # on a pending job — deleting a still-unparsed PDF would strand it
    return con.execute(
        """SELECT r.id, r.path FROM reports r
           WHERE r.sha256 IS NOT NULL AND r.path IS NOT NULL
             AND r.pages > 0
             AND NOT EXISTS (
               SELECT 1 FROM jobs j
               WHERE j.status='pending'
                 AND j.kind IN ('pages','figures','extract')
                 AND CAST(json_extract(j.payload,'$.report_id') AS INT)
                     = r.id)
           ORDER BY r.id LIMIT ?""",
        (limit,)).fetchall()


def run(limit=1000):
    con = db()
    con.execute("PRAGMA busy_timeout=120000")
    rows = compactable(con, limit)
    freed = done = 0
    for r in rows:
        path = r["path"]
        try:
            size = os.path.getsize(path)
            os.unlink(path)
            freed += size
        except OSError:
            pass  # already gone — still clear the row marker
        con.execute("UPDATE reports SET path=NULL WHERE id=?", (r["id"],))
        done += 1
        if done % 100 == 0:
            con.commit()
            print(f"compact: {done}/{len(rows)} reports, "
                  f"{freed / 1e9:.2f}G freed", flush=True)
    con.commit()
    print(f"compact: {done} reports compacted, {freed / 1e9:.2f}G freed",
          flush=True)
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('compact',?,?,?,?)",
        (now(), "", 1, done))
    con.commit()
    return done, freed


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    run(limit)


if __name__ == "__main__":
    main()
