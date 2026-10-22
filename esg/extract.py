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
import hashlib
import json
import re
import signal
import threading
import time

from .esg import db, echo_gate, extract_json_list, gw_chat, gw_up, now


def _lock_retry(fn, tries=12):
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
        rf"scope\s*1[^\d]{{0,60}}{NUM}\s*(?:m|k|t)?t?co[₂2]e\b",
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
        rf"scope\s*3[^\d]{{0,60}}{NUM}\s*(?:m|k|t)?t?co[₂2]e\b",
    ], "tco2e"),
    ("energy_mwh", [
        rf"total\s+(?:energy|electricity)[^\d]{{0,40}}{NUM}\s*(mwh|gwh|twh)",
        rf"total\s+(?:energy|electricity)\s+(?:consumption|use)[^\d]{{0,40}}"
        rf"{NUM}\s*(gj|tj|mmbtu)",
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
    "billion": 1e9, "kt": 1e3, "mtco2e": 1e6, "mt co2e": 1e6,
    "tj": 277.78, "gj": 0.27778, "mmbtu": 0.29307, "mj": 0.00027778,
    "megalit": 1e3, "km3": 1e6,
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
    r"(?:t|mt|kt|kg)\s*co[₂2]e?\b|"  # compact forms: tco2e, mtco2e, kgco2e
    r"(?:mwh|gwh|twh|kwh|megajoules?|gj|mj|mmbtu|tj|pj)|"
    r"(?:m3|m³|cubic\s+met(?:er|re)s?|megalit(?:er|re)s?|lit(?:er|re)s?|km3)|"
    r"(?:%|percent|per\s+cent)|"
    r"(?:g|kg|t)\s*co[₂2]e?/(?:boe|mj|gj)")
# unit families a metric may be drawn from — the number's actual unit must
# be in the metric's family, so "30% of energy use" can't become energy_mwh
_MASS = r"tonnes?|tons?|mt|kt|t\b|co[₂2]e?\b"
_ENERGY = r"mwh|gwh|twh|kwh|megajoules?|gj|mj\b|mmbtu|tj\b|pj\b"
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


class _TableTooSlow(Exception):
    pass


def _on_alarm(*_):
    raise _TableTooSlow()


def table_extract(pdf_path, page_no):
    """Deterministic table tier: pdfplumber rows carrying a metric label
    plus a number+unit cell. Cells are verbatim — higher precision than LLM."""
    try:
        pdf = _plumber(pdf_path)
        if page_no < 1 or page_no > len(pdf.pages):
            return []
        # pathological dense pages can take minutes inside pdfminer —
        # bound it; the LLM tier is the recall net for what this misses
        main_thread = threading.current_thread() is threading.main_thread()
        if main_thread:
            signal.signal(signal.SIGALRM, _on_alarm)
            signal.alarm(20)
        try:
            tables = pdf.pages[page_no - 1].extract_tables()
        finally:
            if main_thread:
                signal.alarm(0)
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
                    "content": LLM_PROMPT.replace("{text}", text[:8000])}])
    if out is None:
        return None
    # truncated responses (max_tokens hit) fail json parse and would mark
    # the job llm_done with partial/zero items — treat as a failed call
    if not out.rstrip().endswith("]"):
        return None
    return [it for it in extract_json_list(out)
            if isinstance(it, dict) and it.get("metric")]


def llm_extract_batch(texts):
    """One call for N pages; items carry a 1-indexed 'page' field."""
    if not gw_up():
        return None
    pages = "\n".join(f"---PAGE {i + 1}---\n{t[:2500]}"
                      for i, t in enumerate(texts))
    out = gw_chat(json_mode=True, max_tokens=3072,
                  messages=[{"role": "user",
                  "content": BATCH_PROMPT.replace("{n}", str(len(texts)))
                  .replace("{pages}", pages)}])
    if out is None:
        return None
    if not out.rstrip().endswith("]"):
        return None  # truncated mid-JSON — never mark jobs done on partials
    return [it for it in extract_json_list(out)
            if isinstance(it, dict) and it.get("metric")]


def process_job(con, job, no_docling=False):
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
    # docling layout tier: borderless tables pdfplumber can't see.
    # --no-docling skips it: CPU-side converts run minutes/page and stall
    # the lanes; those pages fall to pending_llm where the gateway (far
    # faster than CPU docling) picks them up echo-gated.
    if not inserted and pdf_path and CTX_RE.search(text) \
            and not no_docling:
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


_GW_FAIL = object()  # gateway call failed — job must stay pending_llm,
                     # NEVER marked llm_done (was silently losing data when
                     # batch prompts overflowed the server's per-slot ctx)

_TL = threading.local()


def _cache_con():
    """Per-thread sqlite con for the page-text dedup cache — identical page
    text yields identical gated items (verbatim snippets, so the echo-gate
    invariant is preserved by construction). Cuts LLM call volume heavily:
    ESG boilerplate (methodology, GRI index, assurance) repeats across
    thousands of reports."""
    c = getattr(_TL, "con", None)
    if c is None:
        c = db()
        c.execute("PRAGMA busy_timeout=120000")
        _TL.con = c
    return c


def llm_work(job):
    """Worker: read text + gateway call only (no sqlite — threads can't share
    the connection). Returns (job_id, payload, gated_items)."""
    p = json.loads(job["payload"])
    text = _page_text(p)
    if text is None:
        return job["id"], p, []
    ccon = _cache_con()
    sha = hashlib.sha256(text.encode()).hexdigest()
    row = ccon.execute("SELECT items FROM llm_cache WHERE text_sha=?",
                       (sha,)).fetchone()
    if row is not None:
        return job["id"], p, json.loads(row[0])
    rows = llm_extract(text)
    if rows is None:
        return job["id"], p, _GW_FAIL
    items = _gate_items(rows, text)
    try:
        ccon.execute("INSERT OR IGNORE INTO llm_cache VALUES(?,?)",
                     (sha, json.dumps(items)))
        ccon.commit()
    except Exception:
        pass
    return job["id"], p, items


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
    """One gateway call for a batch of UNSEEN page texts; texts already in
    llm_cache reuse their stored items (identical source -> identical
    verbatim snippets -> echo-gate still passes). Returns _GW_FAIL when the
    call itself failed — callers must leave those jobs pending_llm, never
    mark them done. Items route back by 'page' field and verify per page
    (misattributed snippets fail that page's gate -> recall loss only)."""
    parts = [(job, json.loads(job["payload"])) for job in jobs]
    texts = [_page_text(p) for _, p in parts]
    results = [(job["id"], p, []) for (job, p) in parts]
    live = [i for i, t in enumerate(texts) if t]
    ccon = _cache_con()
    missing = []
    for i in live:
        sha = hashlib.sha256(texts[i].encode()).hexdigest()
        row = ccon.execute("SELECT items FROM llm_cache WHERE text_sha=?",
                           (sha,)).fetchone()
        if row is None:
            missing.append((i, sha))
        else:
            results[i] = (parts[i][0]["id"], parts[i][1],
                          json.loads(row[0]))
    if not missing:
        return results
    rows = llm_extract_batch([texts[i] for i, _ in missing])
    if rows is None:
        return _GW_FAIL
    per = {pos: [] for pos in range(len(missing))}
    for it in rows:
        try:
            pi = int(it.get("page", 0)) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= pi < len(missing):
            per[pi].append(it)
    for pos, (i, sha) in enumerate(missing):
        gated = _gate_items(per[pos], texts[i])
        try:
            ccon.execute("INSERT OR IGNORE INTO llm_cache VALUES(?,?)",
                         (sha, json.dumps(gated)))
            ccon.commit()
        except Exception:
            pass
        results[i] = (parts[i][0]["id"], parts[i][1], gated)
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


def _bump_llm_tries(con, job_ids):
    """Gateway failed mid-batch — don't mark jobs done (silent data loss),
    but bound retries so a permanently-too-big page can't loop forever."""
    for jid in job_ids:
        con.execute(
            """UPDATE jobs SET payload=json_set(payload,'$.llm_tries',
               COALESCE(json_extract(payload,'$.llm_tries'),0)+1)
               WHERE id=?""", (jid,))
    con.commit()


def _llm_pass(con, a, shard, order, limit=0):
    """One bounded LLM round over pending_llm jobs. Returns jobs processed
    (0 = queue empty, -1 = gateway down). Called repeatedly by --drain-llm
    so the GPU tier overlaps pass-1 instead of waiting for a full sweep."""
    q2 = ("SELECT id, payload FROM jobs WHERE kind='extract' AND "
          "status='pending_llm' AND "
          "COALESCE(json_extract(payload,'$.llm_tries'),0) < 3"
          f"{shard}{order}")
    if a.llm:
        q2 = ("SELECT id, payload FROM jobs WHERE kind='extract' AND "
              f"status IN ('pending_llm','regex_done'){shard}{order}")
    q2 += f" LIMIT {limit or a.llm_workers * max(a.batch, 1) * 8}"
    jobs = con.execute(q2).fetchall()
    if not jobs:
        return 0
    if not gw_up():
        print(f"gateway down — {len(jobs)} pages stay pending_llm")
        return -1
    from concurrent.futures import ThreadPoolExecutor
    total2 = 0
    with ThreadPoolExecutor(max_workers=a.llm_workers) as ex:
        if a.batch and a.batch > 1:
            chunks = [jobs[i:i + a.batch]
                      for i in range(0, len(jobs), a.batch)]
            for chunk, results in zip(chunks,
                                      ex.map(llm_work_batch, chunks)):
                if results is _GW_FAIL:
                    _bump_llm_tries(con, [j["id"] for j in chunk])
                    continue
                for job_id, p, items in results:
                    total2 += _lock_retry(
                        lambda: llm_insert(con, job_id, p, items))
        else:
            for job_id, p, items in ex.map(llm_work, jobs):
                if items is _GW_FAIL:
                    _bump_llm_tries(con, [job_id])
                    continue
                total2 += _lock_retry(
                    lambda: llm_insert(con, job_id, p, items))
    print(f"extract llm: {total2} metrics on {len(jobs)} pages", flush=True)
    return len(jobs)


def _drain_llm(con, a, shard, order):
    """Continuous pending_llm drain — a dedicated lane that keeps the
    gateway busy while pass-1 shards are still sweeping. Idles 60s when
    the queue is empty; exits after 3 idle rounds (supervisor relaunches)."""
    idle = 0
    while idle < 3:
        n = _llm_pass(con, a, shard, order)
        if n <= 0:
            idle += 1
            time.sleep(60)
        else:
            idle = 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--llm", action="store_true",
                    help="also run LLM on regex_done pages (deeper sweep)")
    ap.add_argument("--llm-workers", type=int, default=4,
                    help="parallel gateway slots (llama-server n_slots)")
    ap.add_argument("--batch", type=int, default=3,
                    help="pages per gateway call (0 = one call per page)")
    ap.add_argument("--no-docling", action="store_true",
                    help="skip the CPU-docling tier (slow on lanes; LLM "
                    "tier covers those pages)")
    ap.add_argument("--shard", type=int, nargs=2, metavar=("K", "N"),
                    help="process only jobs where report_id %% N = K — "
                    "disjoint shards parallelize with no claim protocol")
    ap.add_argument("--drain-llm", action="store_true",
                    help="skip pass-1 entirely; loop the LLM tier over "
                    "pending_llm until it stays empty (dedicated lane)")
    a = ap.parse_args()
    con = db()
    con.execute("PRAGMA busy_timeout=120000")
    con.execute("CREATE TABLE IF NOT EXISTS llm_cache"
                "(text_sha TEXT PRIMARY KEY, items TEXT)")
    con.commit()
    # shard by report_id (not job id): keeps every page of a report in the
    # same shard so the one-entry pdfplumber/fitz caches actually hit;
    # report DESC = newest-cataloged first (breadth coverage lands early).
    rid = "json_extract(payload,'$.report_id')"
    shard = f" AND {rid} % {a.shard[1]} = {a.shard[0]}" if a.shard else ""
    # newest-report-first even unsharded: the drain lane feeds the LLM the
    # most decision-useful pages first while the tail grinds behind
    order = f" ORDER BY {rid} DESC, id"
    if a.drain_llm:
        _drain_llm(con, a, shard, order)
        return
    # pass 1: regex + context tiers on pending and previously-LLM-queued
    # pages. Chunked re-query (not a fetchall snapshot) so jobs enqueued
    # by pages/figures mid-run get picked up without a relaunch.
    q = ("SELECT id, payload FROM jobs WHERE kind='extract' AND "
         f"status IN ('pending','pending_llm'){shard}{order} LIMIT 800")
    total = 0
    rounds = 0
    while True:
        fetched = con.execute(q).fetchall()
        if not fetched or rounds >= (1 if a.limit else 10**9):
            break
        rounds += 1
        for job in fetched:
            total += _lock_retry(
                lambda: process_job(con, job, a.no_docling))
    print(f"extract regex: {total} metrics", flush=True)
    # pass 2: manual/oneshot runs drain the whole pending_llm queue here;
    # lane runs get the same coverage from the dedicated --drain-llm lane
    total2 = 0
    while True:
        n = _llm_pass(con, a, shard, order, limit=a.limit)
        if n <= 0:
            break
        total2 += n
    print(f"extract: {total} metrics inserted")
    con.execute(
        "INSERT OR REPLACE INTO source_snapshots VALUES('extract',?,?,?,?)",
        (now(), "", 1, total))
    con.commit()


if __name__ == "__main__":
    main()
