"""claims.py — video-claims lane from the surviving NYCW corpus.

Source: GitHub climate-week-search data.json = 911 videos + 39,150 windows
[video_idx, start_s, end_s, speaker, text] — speaker-tagged, timestamped.

Pipeline: ingest windows -> claims_corpus; LLM classifies windows that mention
metrics -> claims rows. HARD RULE (v1 design): the model classifies and
rationalizes; `snippet` is ALWAYS retrieved verbatim from the corpus row —
never model-generated. Pins: yt#<video_id>@<start_s>.
"""
import argparse
import json
import os
import re
import urllib.request

from rapidfuzz import fuzz

from .esg import db, echo_gate, extract_json_list, gw_chat, gw_up, now

CORPUS_URL = ("https://raw.githubusercontent.com/TerexitariusStomp/"
              "climate-week-search/main/data.json")
CORPUS_CACHE = "data/source/cws_data.json"
UA = {"User-Agent": "esg-verdicts/1.0"}

METRIC_RE = re.compile(
    r"scope\s*[123]|co2e?|emissions?|net[\s-]?zero|carbon[\s-]?neutral|"
    r"renewable|offset|reduction|methane|\d+%|gigaton|megaton", re.I)


def ingest_corpus(con):
    os.makedirs("data/source", exist_ok=True)
    if not os.path.exists(CORPUS_CACHE):
        req = urllib.request.Request(CORPUS_URL, headers=UA)
        with urllib.request.urlopen(req, timeout=120) as r:
            open(CORPUS_CACHE, "wb").write(r.read())
    d = json.load(open(CORPUS_CACHE))
    videos = d["videos"]
    n = 0
    for w in d["w"]:
        vidx, start, end, speaker, text = w[0], w[1], w[2], w[3], w[4]
        vid = videos.get(str(vidx), [None, None])
        if not vid[0]:
            continue
        con.execute(
            """INSERT OR IGNORE INTO claims_corpus
               (video_id, title, start_s, end_s, speaker, text)
               VALUES(?,?,?,?,?,?)""",
            (vid[0], vid[1], start, end, speaker, text))
        n += 1
    con.commit()
    return n


def org_match(con, speaker, title):
    """Fuzzy-map speaker/org text -> company_id."""
    s = _norm((speaker or "") + " " + (title or ""))
    best, best_id = 0, None
    for r in con.execute("SELECT id, name FROM companies").fetchall():
        sc = fuzz.token_set_ratio(_norm(r["name"]), s)
        if sc > best:
            best, best_id = sc, r["id"]
    return (best_id if best >= 80 else None), best


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


CLASSIFY_PROMPT = """You classify statements from public climate-event videos.
Given this verbatim excerpt, decide if it contains a quantified environmental
claim (target, emissions figure, renewable %, commitment with a number).

Return strict JSON list — one item per claim:
{"has_claim": true, "claim_type": "target|absolute|intensity|renewable|commitment",
 "metric_key": "scope1_mtco2e|scope3_mtco2e|renewable_pct|netzero_target_year|other",
 "value": number|null, "unit": "string|null", "year": int|null,
 "org": "organization the speaker represents or null"}
Return [] if no quantified claim. No explanation, no invented quotes.

---EXCERPT---
{text}"""


def work_window(row):
    """Worker: prefilter + gateway call only (threads share no sqlite)."""
    if not METRIC_RE.search(row["text"] or ""):
        return row, []
    out = gw_chat(json_mode=True, messages=[{"role": "user",
                    "content": CLASSIFY_PROMPT.replace(
                        "{text}", row["text"][:3000])}])
    if out is None:
        return row, None
    good = []
    for it in extract_json_list(out):
        if not it.get("has_claim"):
            continue
        v = it.get("value")
        # value-in-text gate: the claimed number must literally appear
        if v is not None:
            vs = str(v).replace(",", "")
            tx = (row["text"] or "").replace(",", "")
            try:
                vi = str(int(float(vs)))
            except (TypeError, ValueError):
                vi = None
            if vs not in tx and (vi is None or vi not in tx):
                continue
        good.append(it)
    return row, good


def insert_window(con, row, items):
    n = 0
    for it in items or []:
        cid, conf = org_match(con, row["speaker"], row["title"])
        snippet = (row["text"] or "")[:400]
        link = f"https://youtu.be/{row['video_id']}?t={int(row['start_s'])}"
        con.execute(
            """INSERT OR IGNORE INTO claims
               (video_id, start_s, speaker, org, metric_key, claim_type,
                value, unit, date, snippet, event_geo, link, company_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["video_id"], row["start_s"], row["speaker"],
             it.get("org") or row["speaker"], it.get("metric_key"),
             it.get("claim_type"), it.get("value"), it.get("unit"),
             it.get("year"), snippet, "NYC", link, cid))
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", action="store_true", help="only ingest corpus")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    con = db()
    n = ingest_corpus(con)
    print(f"claims: {n} corpus windows ingested")
    if a.ingest:
        return
    if not gw_up():
        print("gateway down — corpus ingested; classification deferred")
        return
    q = ("SELECT id, video_id, title, start_s, end_s, speaker, text "
         "FROM claims_corpus WHERE text IS NOT NULL ORDER BY id")
    if a.limit:
        q += f" LIMIT {a.limit}"
    rows = con.execute(q).fetchall()
    total = 0
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, (row, items) in enumerate(ex.map(work_window, rows)):
            if items is None:
                print("gateway died mid-run — stopping")
                break
            total += insert_window(con, dict(row), items)
            if i % 200 == 0:
                con.commit()  # release the write lock for other stages
    con.commit()
    print(f"claims: {total} claims extracted")
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('claims',?,?,?,?)",
        (now(), "", 7, total))
    con.commit()


if __name__ == "__main__":
    main()
