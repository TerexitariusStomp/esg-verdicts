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
import time

from .esg import db, echo_gate, extract_json_list, gw_chat, gw_up, now


def _lock_retry(fn, tries=6):
    """3 extract shards + harvest lanes share the writer; a slow peer can
    hold the lock past busy_timeout — retry with backoff instead of dying."""
    for t in range(tries):
        try:
            return fn()
        except Exception as e:
            if "locked" in str(e) and t < tries - 1:
                time.sleep(10 * (t + 1))
                continue
            raise

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


# --- context tier: number+unit within window of an ESG label ----------------
# Catches table rows and "57 million tonnes" phrasing the strict patterns miss,
# while staying fully verbatim (snippet is source text by construction).
UNITS = (
    r"(?:(?:million|thousand|billion)\s+)?(?:metric\s+)?"
    r"(?:tonnes?|tons?|mt|kt|t)\s*(?:of\s+)?"
    r"(?:co2e?|carbon(?:\s+dioxide)?(?:\s+equivalent)?|ghg)?|"
    r"(?:mwh|gwh|twh|kwh|megajoules?|gj|mj|mmbtu)|"
    r"(?:m3|cubic\s+met(?:er|re)s?|megalit(?:er|re)s?|lit(?:er|re)s?)|"
    r"(?:%|percent|per\s+cent)|"
    r"(?:g|kg|t)\s*co[₂2]e?/(?:boe|mj|gj)")
# unit families a metric may be drawn from — the number's actual unit must
# be in the metric's family, so "30% of energy use" can't become energy_mwh
_MASS = r"tonnes?|tons?|mt|kt|t\b"
_ENERGY = r"mwh|gwh|twh|kwh|megajoules?|gj|mj\b|mmbtu"
_VOL = r"m3|cubic|megalit|lit(?:er|re)"
_PCT = r"%|percent|per\s+cent"
CTX_RULES = [
    (r"scope\s*1|direct\s+(?:ghg\s+)?emissions", "scope1_mtco2e", _MASS,
     r"co2|carbon|ghg|emission"),
    (r"scope\s*2[^\n]{0,30}location", "scope2_loc_mtco2e", _MASS,
     r"co2|carbon|ghg|emission"),
    (r"scope\s*2[^\n]{0,30}market", "scope2_mkt_mtco2e", _MASS,
     r"co2|carbon|ghg|emission"),
    (r"scope\s*3|indirect\s+(?:ghg\s+)?emissions", "scope3_mtco2e", _MASS,
     r"co2|carbon|ghg|emission"),
    (r"carbon\s+intensity|emissions?\s+intensity", "intensity_kgco2e",
     r"(?:g|kg|t)\s*co[₂2]e?/", None),
    (r"energy|electricity", "energy_mwh", _ENERGY, r"co[₂2]"),
    (r"renewable", "renewable_pct", _PCT, None),
    (r"water", "water_m3", _VOL, r"co[₂2]"),
    (r"waste", "waste_t", _MASS, r"co[₂2]|emission"),
    (r"recycled|recycling", "recycled_pct", _PCT, None),
    (r"reduced|avoided|cut\b", "reduction_tco2e", _MASS,
     None),
]
NUM_NC = r"[\d,]+(?:\.\d+)?"
CTX_RE = re.compile(rf"({NUM_NC})\s*({UNITS})", re.I)
WINDOW = 60


def context_extract(text):
    """Deterministic context-tier: NUM+UNIT whose unit family and nearby
    label agree on a metric. Verbatim by construction — no model involved."""
    out = []
    seen = set()
    for m in CTX_RE.finditer(text):
        raw, unit = m.group(1), m.group(2).lower()
        lo = max(0, m.start() - WINDOW)
        ctx = text[lo:m.end() + WINDOW]
        for pat, key, ufam, neg in CTX_RULES:
            if not re.search(ufam, unit, re.I):
                continue
            if not re.search(pat, ctx, re.I):
                continue
            if neg and re.search(neg, unit, re.I):
                continue
            if re.fullmatch(r"(?:19|20)\d{2}", raw):
                continue
            try:
                val = _num(raw)
            except Exception:
                continue
            if not 1 <= val <= 1e11:
                continue
            val *= _scale_unit(unit, "")
            year = _year_near(text, m.start())
            sig = (key, round(val, 4), year)
            if sig in seen:
                continue
            seen.add(sig)
            out.append({"metric": key, "value": val,
                        "unit": key.split("_")[-1], "year": year,
                        "snippet": ctx.strip()[:240], "tier": "context",
                        "confidence": 0.75})
            break
    return out


_PLUMBER = {"path": None, "pdf": None}


def _plumber(pdf_path):
    """One-entry cache — extract jobs are id-contiguous per report, so the
    same PDF gets opened once instead of once per page (O(pages^2) -> O(1))."""
    if _PLUMBER["path"] != pdf_path:
        if _PLUMBER["pdf"] is not None:
            try:
                _PLUMBER["pdf"].close()
            except Exception:
                pass
        import pdfplumber
        _PLUMBER["pdf"] = pdfplumber.open(pdf_path)
        _PLUMBER["path"] = pdf_path
    return _PLUMBER["pdf"]


def table_extract(pdf_path, page_no):
    """Deterministic table tier: pdfplumber rows carrying a metric label
    plus a number+unit cell. Cells are verbatim — higher precision than LLM."""
    try:
        pdf = _plumber(pdf_path)
        if page_no < 1 or page_no > len(pdf.pages):
            return []
        tables = pdf.pages[page_no - 1].extract_tables()
    except Exception:
        return []
    out, seen = [], set()
    for tbl in tables:
        # year columns from header rows
        hdr_years = {}
        for row in tbl:
            for ci, c in enumerate(row or []):
                if c and re.fullmatch(r"(?:19|20)\d{2}", c.strip()):
                    hdr_years[ci] = int(c.strip())
        for row in tbl:
            cells = [str(c) for c in (row or []) if c]
            rtext = " ".join(cells)
            if not rtext.strip():
                continue
            for ci, cell in enumerate(row or []):
                if not cell:
                    continue
                m = CTX_RE.search(cell)
                if not m:
                    continue
                raw, unit = m.group(1), m.group(2).lower()
                if re.fullmatch(r"(?:19|20)\d{2}", raw):
                    continue
                for pat, key, ufam, neg in CTX_RULES:
                    if not re.search(ufam, unit, re.I):
                        continue
                    if not re.search(pat, rtext, re.I):
                        continue
                    if neg and re.search(neg, unit, re.I):
                        continue
                    try:
                        val = _num(raw)
                    except Exception:
                        continue
                    if not 1 <= val <= 1e11:
                        continue
                    val *= _scale_unit(unit, "")
                    year = hdr_years.get(ci)
                    sig = (key, round(val, 4), year)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    out.append({"metric": key, "value": val,
                                "unit": key.split("_")[-1], "year": year,
                                "snippet": rtext.strip()[:240],
                                "tier": "table", "confidence": 0.8})
                    break
    return out


_DOCLING = None


def _docling_conv():
    global _DOCLING
    if _DOCLING is None:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import (DocumentConverter,
                                                PdfFormatOption)
        opts = PdfPipelineOptions()
        opts.do_table_structure = True
        opts.do_ocr = False  # layout+table only; OCR lane is RapidOCR
        _DOCLING = DocumentConverter(format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    return _DOCLING


_FITZ = {"path": None, "doc": None}


def _fitz(pdf_path):
    """Same one-entry cache for the docling temp-page builder."""
    if _FITZ["path"] != pdf_path:
        if _FITZ["doc"] is not None:
            try:
                _FITZ["doc"].close()
            except Exception:
                pass
        import pymupdf
        _FITZ["doc"] = pymupdf.open(pdf_path)
        _FITZ["path"] = pdf_path
    return _FITZ["doc"]


def docling_extract(pdf_path, page_no):
    """Docling layout tier on a single page — catches borderless tables
    pdfplumber misses. Rows -> CTX_RULES mapping, verbatim cells."""
    import pymupdf
    import tempfile
    import os
    try:
        src = _fitz(pdf_path)
        if page_no < 1 or page_no > src.page_count:
            return []
        one = pymupdf.open()
        one.insert_pdf(src, from_page=page_no - 1, to_page=page_no - 1)
        with tempfile.NamedTemporaryFile(suffix=".pdf",
                                         delete=False) as tf:
            one.save(tf.name)
            tmp = tf.name
    except Exception:
        return []
    try:
        doc = _docling_conv().convert(tmp).document
    except Exception:
        os.unlink(tmp)
        return []
    os.unlink(tmp)
    out, seen = [], set()
    for tbl in getattr(doc, "tables", []):
        try:
            df = tbl.export_to_dataframe(doc=doc)
        except Exception:
            continue
        hdr_years = {}
        for ci, c in enumerate(df.columns):
            if re.fullmatch(r"(?:19|20)\d{2}", str(c).strip()):
                hdr_years[ci] = int(str(c).strip())
        for _, row in df.iterrows():
            cells = [str(c) for c in row if str(c) not in ("nan", "")]
            rtext = " ".join(cells)
            if not rtext.strip():
                continue
            for ci, cell in enumerate(row):
                cell = str(cell)
                m = CTX_RE.search(cell)
                if not m:
                    continue
                raw, unit = m.group(1), m.group(2).lower()
                if re.fullmatch(r"(?:19|20)\d{2}", raw):
                    continue
                for pat, key, ufam, neg in CTX_RULES:
                    if not re.search(ufam, unit, re.I):
                        continue
                    if not re.search(pat, rtext, re.I):
                        continue
                    if neg and re.search(neg, unit, re.I):
                        continue
                    try:
                        val = _num(raw)
                    except Exception:
                        continue
                    if not 1 <= val <= 1e11:
                        continue
                    val *= _scale_unit(unit, "")
                    sig = (key, round(val, 4), hdr_years.get(ci))
                    if sig in seen:
                        continue
                    seen.add(sig)
                    out.append({"metric": key, "value": val,
                                "unit": key.split("_")[-1],
                                "year": hdr_years.get(ci),
                                "snippet": rtext.strip()[:240],
                                "tier": "docling", "confidence": 0.85})
                    break
    return out


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


BATCH_PROMPT = """Extract environmental metrics from each ESG report page below.
For EACH metric return a JSON list item:
{"page": <page index 1-{n}>, "metric": one of scope1_mtco2e,
scope2_loc_mtco2e, scope2_mkt_mtco2e, scope3_mtco2e, energy_mwh,
renewable_pct, water_m3, waste_t, recycled_pct, emissions_intensity,
netzero_target_year,
 "value": number, "unit": "string", "year": int,
 "scope": "1/2/3/null", "target_year": int|null,
 "snippet": "EXACT substring copied verbatim from that page's text",
 "confidence": 0.0-1.0}
Only metrics literally stated. snippet MUST be verbatim. [] if none.
JSON list only.

{pages}"""


def llm_extract(text):
    if not gw_up():
        return None
    out = gw_chat(json_mode=True, messages=[{"role": "user",
                    "content": LLM_PROMPT.replace("{text}", text[:12000])}])
    if out is None:
        return None
    return [it for it in extract_json_list(out)
            if isinstance(it, dict) and it.get("metric")]


def llm_extract_batch(texts):
    """One call for N pages; items carry a 1-indexed 'page' field."""
    if not gw_up():
        return None
    pages = "\n".join(f"---PAGE {i + 1}---\n{t[:4000]}"
                      for i, t in enumerate(texts))
    out = gw_chat(json_mode=True, messages=[{"role": "user",
                  "content": BATCH_PROMPT.replace("{n}", str(len(texts)))
                  .replace("{pages}", pages)}])
    if out is None:
        return None
    return [it for it in extract_json_list(out)
            if isinstance(it, dict) and it.get("metric")]


def process_job(con, job):
    """Regex tier (always) -> LLM tier only if regex found nothing on the page
    (the expensive path is reserved for pages determinism missed)."""
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
    inserted = 0
    for it in regex_extract(text) + context_extract(text):
        if not echo_gate(it["snippet"], text):
            continue
        con.execute(
            """INSERT INTO extractions
               (report_id, metric_key, value, unit, year_covered, scope,
                target_year, page, snippet, figure_crop, confidence, tier, model)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, it["metric"], it["value"], it["unit"], it.get("year"),
             it.get("scope"), it.get("target_year"), page, it["snippet"],
             p.get("figure_crop"), it["confidence"], it["tier"], None))
        inserted += 1
    # table tier: structured cells before paying for the model
    pdf_path = (con.execute("SELECT path FROM reports WHERE id=?",
                            (rid,)).fetchone()
                if not inserted else None)
    if pdf_path:
        for it in table_extract(pdf_path[0], page):
            if not echo_gate(it["snippet"], text):
                continue
            con.execute(
                """INSERT INTO extractions
                   (report_id, metric_key, value, unit, year_covered, scope,
                    target_year, page, snippet, figure_crop, confidence,
                    tier, model)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, it["metric"], it["value"], it["unit"], it.get("year"),
                 it.get("scope"), it.get("target_year"), page, it["snippet"],
                 p.get("figure_crop"), it["confidence"], "table", None))
            inserted += 1
    # docling layout tier: borderless tables pdfplumber can't see
    if not inserted and pdf_path and CTX_RE.search(text):
        for it in docling_extract(pdf_path[0], page):
            if not echo_gate(it["snippet"], text):
                continue
            con.execute(
                """INSERT INTO extractions
                   (report_id, metric_key, value, unit, year_covered, scope,
                    target_year, page, snippet, figure_crop, confidence,
                    tier, model)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, it["metric"], it["value"], it["unit"], it.get("year"),
                 it.get("scope"), it.get("target_year"), page, it["snippet"],
                 p.get("figure_crop"), it["confidence"], "docling", None))
            inserted += 1
    # no NUM+UNIT on the page -> no metric can exist; skip LLM entirely
    if not inserted and not CTX_RE.search(text):
        inserted = -1
    status = "regex_done" if inserted else "pending_llm"
    con.execute("UPDATE jobs SET status=? WHERE id=?", (status, job["id"]))
    con.commit()
    return inserted


def llm_work(job):
    """Worker: read text + gateway call only (no sqlite — threads can't share
    the connection). Returns (job_id, payload, gated_items)."""
    p = json.loads(job["payload"])
    text = _page_text(p)
    if text is None:
        return job["id"], p, None
    return job["id"], p, _gate_items(llm_extract(text) or [], text)


def _page_text(p):
    src_path = f"data/pages/{p['report_id']}/{p['page']}.txt"
    if p.get("figure_crop"):
        src_path = p["figure_crop"] + ".ocr.txt"
    try:
        return open(src_path).read()
    except OSError:
        return None


def _gate_items(llm_rows, text):
    good = []
    for it in llm_rows:
        snip = str(it.get("snippet", ""))
        if not echo_gate(snip, text):
            continue
        v = it.get("value")
        if v is not None:
            vs = str(v).replace(",", "")
            sn = snip.replace(",", "")
            try:
                vi = str(int(float(vs)))
            except (TypeError, ValueError):
                vi = None
            if vs not in sn and (vi is None or vi not in sn):
                continue
        good.append(it)
    return good


def llm_work_batch(jobs):
    """One gateway call for a batch of pages; items routed back by 'page'
    field and verified per page (misattributed snippets fail that page's
    echo gate -> recall loss only, never precision)."""
    parts = [(job, json.loads(job["payload"])) for job in jobs]
    texts = [_page_text(p) for _, p in parts]
    results = [(job["id"], p, []) for (job, p) in parts]
    live = [i for i, t in enumerate(texts) if t]
    rows = llm_extract_batch([texts[i] for i in live])
    if not rows:
        return results
    per = {i: [] for i in range(len(jobs))}
    for it in rows:
        try:
            pi = int(it.get("page", 0)) - 1
        except (TypeError, ValueError):
            continue
        slot = live[pi] if 0 <= pi < len(live) else None
        if slot is not None:
            per[slot].append(it)
    for i, (job, p) in enumerate(parts):
        if texts[i]:
            results[i] = (job["id"], p, _gate_items(per[i], texts[i]))
    return results


def llm_insert(con, job_id, p, items):
    n = 0
    for it in items or []:
        con.execute(
            """INSERT INTO extractions
               (report_id, metric_key, value, unit, year_covered, scope,
                target_year, page, snippet, figure_crop, confidence,
                tier, model)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p["report_id"], it["metric"], it.get("value"), it.get("unit"),
             it.get("year"), it.get("scope"), it.get("target_year"),
             p["page"], str(it.get("snippet", "")), p.get("figure_crop"),
             it.get("confidence", 0.5), "llm", "gateway"))
        n += 1
    con.execute("UPDATE jobs SET status='llm_done' WHERE id=?", (job_id,))
    con.commit()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--llm", action="store_true",
                    help="also run LLM on regex_done pages (deeper sweep)")
    ap.add_argument("--llm-workers", type=int, default=4,
                    help="parallel gateway slots (llama-server n_slots)")
    ap.add_argument("--batch", type=int, default=3,
                    help="pages per gateway call (0 = one call per page)")
    ap.add_argument("--shard", type=int, nargs=2, metavar=("K", "N"),
                    help="process only jobs where id %% N = K — disjoint "
                    "shards parallelize pass-1 with no claim protocol")
    a = ap.parse_args()
    con = db()
    con.execute("PRAGMA busy_timeout=120000")
    shard = f" AND id % {a.shard[1]} = {a.shard[0]}" if a.shard else ""
    # pass 1: regex + context tiers on pending and previously-LLM-queued pages
    q = ("SELECT id, payload FROM jobs WHERE kind='extract' AND "
         f"status IN ('pending','pending_llm'){shard} ORDER BY id")
    if a.limit:
        q += f" LIMIT {a.limit}"
    total = 0
    for job in con.execute(q).fetchall():
        total += _lock_retry(lambda: process_job(con, job))
    print(f"extract regex: {total} metrics")
    # pass 2: LLM only where regex missed (or on all done pages if --llm),
    # parallel across the gateway's slots
    q2 = ("SELECT id, payload FROM jobs WHERE kind='extract' AND "
          f"status='pending_llm'{shard} ORDER BY id")
    if a.llm:
        q2 = ("SELECT id, payload FROM jobs WHERE kind='extract' AND "
              f"status IN ('pending_llm','regex_done'){shard} ORDER BY id")
    if a.limit:
        q2 += f" LIMIT {a.limit}"
    jobs = con.execute(q2).fetchall()
    if jobs and gw_up():
        from concurrent.futures import ThreadPoolExecutor
        total2 = 0
        with ThreadPoolExecutor(max_workers=a.llm_workers) as ex:
            if a.batch and a.batch > 1:
                chunks = [jobs[i:i + a.batch]
                          for i in range(0, len(jobs), a.batch)]
                for results in ex.map(llm_work_batch, chunks):
                    for job_id, p, items in results:
                        total2 += _lock_retry(
                            lambda: llm_insert(con, job_id, p, items))
            else:
                for job_id, p, items in ex.map(llm_work, jobs):
                    total2 += _lock_retry(
                        lambda: llm_insert(con, job_id, p, items))
        print(f"extract llm: {total2} metrics on {len(jobs)} pages")
        total += total2
    elif jobs:
        print(f"gateway down — {len(jobs)} pages stay pending_llm")
    print(f"extract: {total} metrics inserted")
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('extract',?,?,?,?)",
        (now(), "", 1, total))
    con.commit()


if __name__ == "__main__":
    main()
