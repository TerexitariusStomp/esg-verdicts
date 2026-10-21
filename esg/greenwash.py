"""greenwash.py — taxonomy mining on the video corpus (v1's separate workstream).

Categories (preserved):
  greenwash — vague pledges, distant net-zero, offsets-as-solution, empty slogans
  admission — self-incriminating/contradictory statements
  absurd    — factually wrong, circular, or ridiculous claims
  callout   — speakers naming greed/hypocrisy/greenwashing outright

V1 bug: produced zero hits on known positives. Debug path baked in:
  --validate POSITIVE_VIDEO_ID runs serially, logs raw LLM responses and
  parse failures, and only then sweeps. Never run a big sweep before --validate
  prints hits on a known-positive window.
"""
import argparse
import json
import re

from .esg import db, extract_json_list, gw_chat, gw_up, now

TAXONOMY_PROMPT = """Classify statements from public climate-event videos.

Categories (pick all that apply):
- greenwash: vague commitments, empty pledges, distant net-zero claims,
  offsets presented as solutions, "part of the solution" language,
  misleading clean/sustainable claims
- admission: self-incriminating or contradictory statements
- absurd: factually wrong, circular, or ridiculous claims
- callout: explicitly naming greed, hypocrisy, greenwashing, or wrongdoing

Return strict JSON list:
{"category": "greenwash|admission|absurd|callout",
 "severity": "low|medium|high", "rationale": "one line"}
[] if none apply. No invented quotes.

---EXCERPT---
{text}"""

TRIG = re.compile(
    r"net[\s-]?zero|carbon[\s-]?neutral|offset|sustainable|green|clean energy|"
    r"solution|commitment|pledge|responsib|greenwash|hypocris|greed|"
    r"100%|by 20[0-9]{2}|reduction|scope\s*3", re.I)


def classify(text):
    out = gw_chat(json_mode=True, messages=[{"role": "user",
                    "content": TAXONOMY_PROMPT.replace(
                        "{text}", text[:3000])}])
    return out


def run_window(con, row):
    if not TRIG.search(row["text"] or ""):
        return []
    raw = classify(row["text"])
    if raw is None:
        return None
    items = extract_json_list(raw)
    hits = []
    for it in items:
        if it.get("category") in ("greenwash", "admission", "absurd", "callout"):
            hits.append(it)
            con.execute(
                """INSERT OR IGNORE INTO claims
                   (video_id, start_s, speaker, org, metric_key, claim_type,
                    value, unit, date, snippet, event_geo, link)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["video_id"], row["start_s"], row["speaker"],
                 row["speaker"], "taxonomy", it["category"], None, None,
                 None, (row["text"] or "")[:400], "NYC",
                 f"https://youtu.be/{row['video_id']}?t={int(row['start_s'])}"))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", default="",
                    help="video_id with known positives — serial run, verbose")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    con = db()
    if not gw_up():
        print("gateway down — greenwash deferred")
        return
    if a.validate:
        rows = con.execute(
            "SELECT * FROM claims_corpus WHERE video_id=? ORDER BY start_s",
            (a.validate,)).fetchall()
        print(f"validate: {len(rows)} windows in {a.validate}")
        hits = 0
        for row in rows:
            raw = classify(row["text"]) if TRIG.search(row["text"] or "") else "[]"
            items = extract_json_list(raw)
            if items:
                hits += len(items)
                print(f"  @{row['start_s']}s -> {items}")
        print(f"validate: {hits} hits "
              f"({'OK — safe to sweep' if hits else 'ZERO — debug before sweep'})")
        return
    q = ("SELECT id, video_id, title, start_s, end_s, speaker, text "
         "FROM claims_corpus WHERE text IS NOT NULL ORDER BY id")
    if a.limit:
        q += f" LIMIT {a.limit}"
    total = 0
    for row in con.execute(q).fetchall():
        r = run_window(con, dict(row))
        if r is None:
            print("gateway died mid-run — stopping")
            break
        total += len(r)
    con.commit()
    print(f"greenwash: {total} taxonomy hits")
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('greenwash',?,?,?,?)",
        (now(), "", 7, total))
    con.commit()


if __name__ == "__main__":
    main()
