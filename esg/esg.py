"""Shared ESG pipeline core: DB schema, integrity primitives, priority scoring,
LLM gateway client. Fetch/throttle machinery lives in Scrapy (catalog/harvest).

This module is intentionally small — it owns only the invariants that must
be ours: the schema, echo-gating, custody manifests, and value-priority.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.request

DB = os.environ.get("ESG_DB", "data/esg.db")
GW = os.environ.get("ESG_GW", "http://127.0.0.1:8080/v1/chat/completions")

# ---------------------------------------------------------------- schema ---

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies(
  id INTEGER PRIMARY KEY, name TEXT, ticker TEXT, lei TEXT,
  country TEXT, sector TEXT, sasb_industry TEXT,
  hq_lat REAL, hq_lon REAL, market_cap REAL,
  cik TEXT, emissions_rank INTEGER, litigation_count INTEGER DEFAULT 0,
  priority REAL DEFAULT 0, UNIQUE(name, ticker));
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY, company_id INTEGER, year INTEGER,
  kind TEXT, url TEXT, path TEXT, sha256 TEXT, pages INTEGER,
  parse_tier TEXT, fetched_at TEXT, acquired_at TEXT,
  acquisition_method TEXT, UNIQUE(company_id, year, url));
CREATE TABLE IF NOT EXISTS source_snapshots(
  source TEXT PRIMARY KEY, last_success TEXT, hash TEXT,
  cadence_days INTEGER, records INTEGER);
CREATE TABLE IF NOT EXISTS extractions(
  id INTEGER PRIMARY KEY, report_id INTEGER, metric_key TEXT,
  value REAL, unit TEXT, year_covered INTEGER, scope TEXT,
  boundary TEXT, standard TEXT, target_year INTEGER, region TEXT,
  page INTEGER, snippet TEXT, figure_crop TEXT,
  confidence REAL, tier TEXT, model TEXT, verified INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_ext ON extractions(report_id, metric_key);
CREATE TABLE IF NOT EXISTS facilities(
  id INTEGER PRIMARY KEY, company_id INTEGER, name TEXT, owner TEXT,
  lat REAL, lon REAL, country TEXT, region TEXT, sector TEXT,
  source TEXT, external_id TEXT, emissions REAL, capacity REAL,
  status TEXT, water_risk REAL, flood_risk REAL, year INTEGER,
  match_confidence REAL, UNIQUE(source, external_id));
CREATE INDEX IF NOT EXISTS ix_fac ON facilities(company_id);
CREATE TABLE IF NOT EXISTS verification(
  id INTEGER PRIMARY KEY, company_id INTEGER, metric TEXT,
  claimed REAL, ground_truth REAL, source TEXT,
  divergence_pct REAL, flag TEXT, year INTEGER,
  UNIQUE(company_id, metric, source, year));
CREATE TABLE IF NOT EXISTS claims(
  id INTEGER PRIMARY KEY, video_id TEXT, start_s REAL,
  speaker TEXT, org TEXT, metric_key TEXT, claim_type TEXT,
  value REAL, unit TEXT, date TEXT, snippet TEXT, event_geo TEXT,
  link TEXT, company_id INTEGER, UNIQUE(video_id, start_s));
CREATE TABLE IF NOT EXISTS litigation(
  id INTEGER PRIMARY KEY, company_id INTEGER, case_id TEXT,
  type TEXT, filed TEXT, status TEXT, snippet TEXT,
  UNIQUE(company_id, case_id));
CREATE TABLE IF NOT EXISTS benchmarks(
  id INTEGER PRIMARY KEY, company_id INTEGER, source TEXT,
  metric TEXT, value TEXT, year INTEGER, url TEXT,
  UNIQUE(company_id, source, metric, year));
CREATE TABLE IF NOT EXISTS flags(
  id INTEGER PRIMARY KEY, company_id INTEGER, flag TEXT,
  severity TEXT, posture TEXT, basis TEXT, pin TEXT,
  year INTEGER, publishable INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(company_id, flag, year, basis));
CREATE TABLE IF NOT EXISTS review_holds(
  id INTEGER PRIMARY KEY, flag_id INTEGER, reason TEXT,
  held_at TEXT DEFAULT (datetime('now')),
  acked_at TEXT, acked_by TEXT, UNIQUE(flag_id));
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, kind TEXT, status TEXT,
  payload TEXT, attempts INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(kind, payload));
CREATE INDEX IF NOT EXISTS ix_jobs ON jobs(status, kind);
CREATE TABLE IF NOT EXISTS results(
  id INTEGER PRIMARY KEY, job_id INTEGER, unit_id TEXT,
  content_hash TEXT, node TEXT, payload TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(unit_id, content_hash));
CREATE TABLE IF NOT EXISTS slug_map(
  source TEXT, slug TEXT, company_id INTEGER,
  UNIQUE(source, slug));
CREATE TABLE IF NOT EXISTS claims_corpus(
  id INTEGER PRIMARY KEY, video_id TEXT, title TEXT,
  start_s REAL, end_s REAL, speaker TEXT, text TEXT,
  UNIQUE(video_id, start_s));
"""


def db(path=None):
    con = sqlite3.connect(path or DB)
    con.row_factory = sqlite3.Row
    os.makedirs(os.path.dirname(path or DB) or ".", exist_ok=True)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    return con


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------ integrity ---

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str, bufsize=1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(bufsize), b""):
            h.update(chunk)
    return h.hexdigest()


def echo_gate(snippet: str, source_text: str) -> bool:
    """The core integrity invariant: a snippet may only be published if it is a
    verbatim substring of the source document text (whitespace-normalized both
    sides — PDF extraction mangles spacing, not characters)."""
    if not snippet or not source_text:
        return False
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    return norm(snippet) in norm(source_text)


def manifest_append(mf_path: str, artifact_path: str, kind: str, extra=None):
    """Hash-chain manifest: each line = sha256(prev_line||artifact_sha256) so the
    chain itself is tamper-evident; append-only."""
    art_hash = sha256_file(artifact_path)
    prev = ""
    if os.path.exists(mf_path):
        with open(mf_path) as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        if lines:
            prev = lines[-1].split()[0]
    chain = hashlib.sha256((prev + art_hash).encode()).hexdigest()
    with open(mf_path, "a") as f:
        f.write(f"{chain} {art_hash} {kind} {artifact_path} {now()}"
                + (f" {json.dumps(extra, sort_keys=True)}" if extra else "") + "\n")
    return art_hash


# ------------------------------------------------------------ priority ----

def recompute_priority(con):
    """Value ordering for every queue: Climate TRACE emissions rank x litigation
    presence x catalog coverage. Biggest emitters already being sued go first."""
    con.execute("""
      UPDATE companies SET priority =
        COALESCE(emissions_rank, 100000) * -1.0
        + COALESCE(litigation_count, 0) * 5000.0
        + (SELECT COUNT(*) FROM reports r WHERE r.company_id = companies.id) * 50.0
    """)


def priority_order(con, limit=None):
    q = "SELECT id, name, priority FROM companies ORDER BY priority DESC, id"
    if limit:
        q += f" LIMIT {int(limit)}"
    return con.execute(q).fetchall()


# ------------------------------------------------------------- LLM gw ------

def gw_up(timeout=3) -> bool:
    try:
        with urllib.request.urlopen(
                GW.replace("/chat/completions", "/models"), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def gw_chat(messages, model="auto", temperature=0.0, timeout=120):
    """OpenAI-compatible local gateway (frelm). Returns content str or None."""
    body = json.dumps({"model": model, "messages": messages,
                       "temperature": temperature}).encode()
    req = urllib.request.Request(
        GW, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
        return out["choices"][0]["message"]["content"]
    except Exception:
        return None


def extract_json_list(text: str):
    """Tolerant JSON-list parse for LLM output."""
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, list) else [v]
    except Exception:
        return []
