"""extract.py — two-tier metric extraction. This is the ~250 LOC of core IP.

Tier-1: deterministic regex pack — scope1/2/3, scope2 loc/mkt, energy_mwh,
        renewable_pct, water_m3, waste_t, recycled_pct, emissions_intensity,
        netzero_target_year. No LLM needed; runs always.
Tier-2: LLM (instructor-style strict JSON) for metrics regex missed —
        EVERY snippet echo-gated against the page text before insert.
        Gateway down -> jobs stay 'regex_done' for later upgrade.

Verbatim rule: a row lands only if snippet is a substring of source text
(whitespace-normalized). The verify gate re-checks this offline.
"""
import argparse
import json
import re

from .esg import db, echo_gate, extract_json_list, gw_chat, gw_up, now

NUM = r"([\d,]+(?:\.\d+)?)"
YEAR = r"(20[0-2]\d)"

METRIC_PATTERNS = [
    ("scope1_mtco2e", [
        rf"scope\s*1[^\d]{{0,60}}{NUM}\s*(?:million\s+|thousand\s+)?"
        r"(?:metric\s+)?(?:tons?|mt|kt)\s*(?:of\s+)?co2e?",
        rf"scope\s*1\s*(?:ghg\s+)?emissions?[^\d]{{0,40}}{NUM}",
    ], "tco2e"),
    ("scope2_loc_mtco2e", [
        rf"scope\s*2[^\d]{{0,20}}location[^\d]{{0,60}}{NUM}\s*[\w\s]{{0,20}}co2e?",
    ], "tco2e"),
    ("scope2_mkt_mtco2e", [
        rf"scope\s*2[^\d]{{0,20}}market[^\d]{{0,60}}{NUM}\s*[\w\s]{{0,20}}co2e?",
    ], "tco2e"),
    ("scope3_mtco2e", [
        rf"scope\s*3[^\d]{{0,60}}{NUM}\s*(?:million\s+|thousand\s+)?"
        r"(?:metric\s+)?(?:tons?|mt|kt)\s*(?:of\s+)?co2e?",
    ], "tco2e"),
    ("energy_mwh", [
        rf"total\s+(?:energy|electricity)[^\d]{{0,40}}{NUM}\s*(mwh|gwh|twh)",
    ], "mwh"),
    ("renewable_pct", [
        rf"{NUM}\s*%\s*(?:of\s+(?:total\s+)?(?:energy|electricity)\s+)"
        r"(?:from\s+)?renewable",
        rf"renewable\s+(?:energy|electricity)[^\d]{{0,30}}{NUM}\s*%",
    ], "pct"),
    ("water_m3", [
        rf"(?:total\s+)?water\s+(?:withdrawal|consumption|use)[^\d]{{0,40}}"
        rf"{NUM}\s*(m3|megaliters?|thousand\s+m3|million\s+m3|billion\s+m3)",
    ], "m3"),
    ("waste_t", [
        rf"(?:total\s+)?waste[^\d]{{0,30}}{NUM}\s*(?:metric\s+)?"
        r"(tons?|tonnes?|mt|kt)",
    ], "t"),
    ("recycled_pct", [
        rf"{NUM}\s*%\s*(?:of\s+)?waste[^\d]{{0,20}}recycled",
        rf"recycled[^\d]{{0,30}}{NUM}\s*%\s*(?:of\s+)?(?:total\s+)?waste",
    ], "pct"),
    ("emissions_intensity", [
        rf"{NUM}\s*(?:mt|metric tons?)\s*co2e?\s*per\s*"
        r"(million|billion|\$?\s*\d+\s*million)?[\w\s]{0,30}"
        r"(revenue|unit|employee|production)",
    ], "intensity"),
    ("netzero_target_year", [
        rf"net[\s-]?zero[^\d]{{0,40}}by\s*{YEAR}",
        rf"carbon[\s-]?neutral[^\d]{{0,40}}by\s*{YEAR}",
    ], "year"),
]

UNIT_SCALE = {
    "gwh": 1e3, "twh": 1e6, "million": 1e6, "thousand": 1e3,
    "billion": 1e9, "kt": 1e3,
}


def _num(s):
    return float(s.replace(",", ""))


def _scale_unit(text, base):
    t = text.lower()
    for k, f in UNIT_SCALE.items():
        if k in t:
            return f
    return 1.0


def _year_near(text, pos):
    """Closest plausible reporting year within 400 chars of the match."""
    best = None
    for m in re.finditer(YEAR, text):
        if best is None or abs(m.start() - pos) < abs(best[1] - pos):
            best = (int(m.group(1)), m.start())
    return best[0] if best else None


def snippet_of(text, m, span=160):
    return text[max(0, m.start() - 40):m.end() + span].strip()


def regex_extract(text, report_year=None):
    """Deterministic tier. Returns list of metric dicts w/ verbatim snippets."""
    out = []
    seen = set()
    for key, pats, unit in METRIC_PATTERNS:
        for pat in pats:
            for m in re.finditer(pat, text, re.I):
                try:
                    if key == "netzero_target_year":
                        val = int(m.group(1))
                    else:
                        val = _num(m.group(1))
                except Exception:
                    continue
                snip = snippet_of(text, m)
                if key != "netzero_target_year" and unit in ("mwh", "tco2e", "m3"):
                    val *= _scale_unit(snip, unit)
                year = _year_near(text, m.start()) or report_year
                sig = (key, round(val, 4), year)
                if sig in seen:
                    continue
                seen.add(sig)
                out.append({"metric": key, "value": val, "unit": unit,
                            "year": year, "snippet": snip, "tier": "regex",
                            "confidence": 0.9})
                break  # first hit per pattern is enough
    return out


LLM_PROMPT = """Extract environmental metrics from this ESG report page.
For EACH metric return a JSON list item:
{"metric": one of scope1_mtco2e, scope2_loc_mtco2e, scope2_mkt_mtco2e,
scope3_mtco2e, energy_mwh, renewable_pct, water_m3, waste_t, recycled_pct,
emissions_intensity, netzero_target_year,
 "value": number, "unit": "string", "year": int,
 "scope": "1/2/3/null", "target_year": int|null,
 "snippet": "EXACT substring copied verbatim from the page text",
 "confidence": 0.0-1.0}
Only metrics literally stated. snippet MUST be verbatim. [] if none.
JSON list only.

---PAGE---
{text}"""


def llm_extract(text):
    if not gw_up():
        return None
    out = gw_chat([{"role": "user", "content": LLM_PROMPT.format(text=text[:12000])}])
    if out is None:
        return None
    return [it for it in extract_json_list(out)
            if isinstance(it, dict) and it.get("metric")]


def process_job(con, job):
    p = json.loads(job["payload"])
    rid, page = p["report_id"], p["page"]
    # source text: page txt, or OCR text for figure jobs
    src_path = f"data/pages/{rid}/{page}.txt"
    if p.get("figure_crop"):
        src_path = p["figure_crop"] + ".ocr.txt"
    try:
        text = open(src_path).read()
    except OSError:
        con.execute("UPDATE jobs SET status='no_text' WHERE id=?", (job["id"],))
        con.commit()
        return 0
    rows = regex_extract(text)
    inserted = 0
    for it in rows:
        if not echo_gate(it["snippet"], text):
            continue
        con.execute(
            """INSERT INTO extractions
               (report_id, metric_key, value, unit, year_covered, scope,
                target_year, page, snippet, figure_crop, confidence, tier, model)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, it["metric"], it["value"], it["unit"], it.get("year"),
             it.get("scope"), it.get("target_year"), page, it["snippet"],
             p.get("figure_crop"), it["confidence"], "regex", None))
        inserted += 1
    # LLM tier — only if gateway up
    llm_rows = llm_extract(text)
    status = "regex_done"
    if llm_rows is not None:
        status = "llm_done"
        for it in llm_rows:
            snip = str(it.get("snippet", ""))
            if not echo_gate(snip, text):
                continue
            con.execute(
                """INSERT INTO extractions
                   (report_id, metric_key, value, unit, year_covered, scope,
                    target_year, page, snippet, figure_crop, confidence,
                    tier, model)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, it["metric"], it.get("value"), it.get("unit"),
                 it.get("year"), it.get("scope"), it.get("target_year"),
                 page, snip, p.get("figure_crop"),
                 it.get("confidence", 0.5), "llm", "gateway"))
            inserted += 1
    con.execute("UPDATE jobs SET status=? WHERE id=?", (status, job["id"]))
    con.commit()
    return inserted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--llm", action="store_true")
    a = ap.parse_args()
    con = db()
    q = ("SELECT id, payload FROM jobs WHERE kind='extract' AND "
         "status IN ('pending','regex_done') ORDER BY id")
    if not a.llm:
        q = ("SELECT id, payload FROM jobs WHERE kind='extract' AND "
             "status='pending' ORDER BY id")
    if a.limit:
        q += f" LIMIT {a.limit}"
    total = 0
    for job in con.execute(q).fetchall():
        total += process_job(con, job)
    print(f"extract: {total} metrics inserted")
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('extract',?,?,?,?)",
        (now(), "", 1, total))
    con.commit()


if __name__ == "__main__":
    main()
