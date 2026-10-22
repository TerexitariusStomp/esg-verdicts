"""harvest.py — idempotent PDF downloader: reports rows -> SHA-256
content-addressed files in data/raw/.

Transport is curl subprocess, not urllib/scrapy: responsibilityreports' edge
blocks the Twisted/OpenSSL TLS fingerprint (JA3) with nginx 403s even on good
Tor exits, while curl through the same exit succeeds. ESG_PROXY (http CONNECT,
e.g. tor HTTPTunnelPort :9080) routes through Tor; a 403 triggers stem NEWNYM
rotation and the request is retried on a fresh exit (waits for a live exit —
v1's bug was failing over to a dead path).

Resumable: sha256 IS NULL selects unfetched rows; INSERT OR REPLACE on jobs.
"""
import argparse
import json
import os
import subprocess
import time

from .esg import db, now, sha256_bytes

RAW = "data/raw"
# RR's edge 403s non-browser UAs on /HostedData/ paths; the files are public
# and any browser downloads them, so identify as one (honest Referer kept).
UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 "
      "Firefox/128.0")
REFERER = "https://www.responsibilityreports.com/"
MAX_PDF = 300 * 1024 * 1024


def _nym():
    try:
        from stem.control import Controller
        c = Controller.from_port(port=int(
            os.environ.get("ESG_TOR_CTRL", "9051")))
        c.authenticate()
        c.signal("NEWNYM")
        time.sleep(8)  # wait for a live exit
    except Exception:
        time.sleep(15)  # no control port — just cool down


def curl_fetch(url, proxy=None, timeout=300):
    """curl subprocess -> (status, body, returncode, elapsed). TLS fingerprint
    matches a real client; 403 means a bad exit, rotate and let caller retry.
    A curl timeout (rc 28) can still print %{http_code}=200 with a partial
    body — the caller MUST check rc before trusting status."""
    cmd = ["curl", "-sL", "-m", str(timeout), "-o", "-",
           "-w", "\n%{http_code}", "-A", UA, "-e", REFERER,
           "--max-filesize", str(MAX_PDF)]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(url)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 30)
        body, _, code = p.stdout.rpartition(b"\n")
        status = int(code.strip() or 0)
        if p.returncode != 0:
            return 0, b"", p.returncode, time.time() - t0
        return status, body, 0, time.time() - t0
    except Exception:
        return 0, b"", -1, time.time() - t0


def sha256_store_put(data):
    if len(data) < 10_000 or data[:5] != b"%PDF-":
        raise ValueError("not a pdf")
    if b"%%EOF" not in data[-1024:]:
        raise ValueError("truncated pdf (no %%EOF trailer)")
    sha = sha256_bytes(data)
    os.makedirs(RAW, exist_ok=True)
    path = f"{RAW}/{sha}.pdf"
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return path, sha


def pending(con, limit=0):
    # Tor lane owns RR-hosted PDFs ONLY — issuer-hosted rows (dataneed
    # catalog, annualreports) go through the direct --issuer lane; pulling
    # thousands of foreign domains through tor exits is slow and pointless
    q = """SELECT r.id, r.url FROM reports r
           LEFT JOIN companies c ON c.id = r.company_id
           WHERE r.sha256 IS NULL
             AND r.url LIKE '%responsibilityreports.com/%.pdf'
           ORDER BY
             (SELECT COUNT(*) FROM reports r2
              WHERE r2.company_id IS r.company_id AND r2.url LIKE '%.pdf'
                AND (r2.year > r.year
                     OR (r2.year = r.year AND r2.id < r.id))) ASC,
             COALESCE(c.priority,0) DESC, r.id"""
    if limit:
        q += f" LIMIT {limit}"
    return con.execute(q).fetchall()


def pending_wayback(con, limit=0):
    """RR-hosted PDFs only, newest-cataloged first — the Tor lanes take
    breadth order, so reverse-id order minimizes row collisions."""
    q = """SELECT r.id, r.url FROM reports r
           WHERE r.sha256 IS NULL
             AND r.url LIKE '%responsibilityreports.com/HostedData/%.pdf'
             AND r.id NOT IN (
               SELECT CAST(json_extract(payload,'$.report_id') AS INT)
               FROM jobs WHERE kind='harvest' AND status='wb_miss')
           ORDER BY r.id DESC"""
    if limit:
        q += f" LIMIT {limit}"
    return con.execute(q).fetchall()


WB = "https://web.archive.org/web/2id_/"


class _Pace:
    """AIMD throttle probe: relax after sustained success, back off on any
    refusal — converges on the source's max tolerated rate without a ban."""
    def __init__(self, start):
        self.v = start
        self.streak = 0

    def ok(self):
        self.streak += 1
        if self.streak >= 200 and self.v > 0.8:
            self.v = max(0.8, self.v - 0.2)
            self.streak = 0

    def bad(self):
        self.streak = 0
        self.v = min(5.0, self.v + 0.5)


def fetch_one(r, proxy, retries=4):
    """One report: retries with NEWNYM rotation on 403 or a stalled/
    timed-out circuit (a dying exit can crawl at ~10KB/s and every lane
    shares it). Returns (report_id, status, body)."""
    for attempt in range(retries):
        status, body, rc, elapsed = curl_fetch(r["url"], proxy)
        if status == 200 and rc == 0:
            return r["id"], status, body
        if status in (404, 410):
            return r["id"], status, b""  # real answer — don't burn retries
        # 403/429/503 = throttled or dead exit; rc!=0 = conn/TLS failure;
        # elapsed>90 = stalled circuit. All need a fresh exit, not backoff.
        if proxy and (status in (403, 429, 503) or rc != 0 or elapsed > 90):
            _nym()
        else:
            time.sleep(min(20, 2 ** attempt * 2))
    return r["id"], status, b""


def fetch_wayback(r, pace, timeout=90, proxy=None):
    """Wayback first: IA serves archived RR PDFs at multi-MB/s without
    touching RR's rate-limiting edge. '2' = latest capture, 'id_' = raw
    bytes (no toolbar rewrite). A 404 means 'not archived' — the caller
    must NOT treat it as a failed fetch; the Tor lane still owns the row.
    pace = shared _Pace (AIMD): IA soft-bans bursty IPs even at low worker
    counts, so the rate self-tunes instead of holding a guessed constant.
    proxy=ESG_PROXY sends the fetch through Tor exits — per-exit rates stay
    polite while aggregate throughput scales with worker count; a refused
    or stalled circuit triggers NEWNYM like the RR lane."""
    time.sleep(pace.v)
    status, body, rc, elapsed = curl_fetch(
        WB + r["url"], proxy=proxy, timeout=timeout)
    if proxy and (status in (403, 429, 503) or rc != 0 or elapsed > 90):
        _nym()  # bad exit or IA throttling this circuit — rotate
    return r["id"], status, body


def _write_locked(con, sql, params, tries=8):
    """Wayback+tor lanes share the writer with extract — a slow batch can
    hold the lock past busy_timeout; retry instead of dying."""
    for t in range(tries):
        try:
            con.execute(sql, params)
            con.commit()
            return True
        except Exception as e:
            if "locked" in str(e) and t < tries - 1:
                time.sleep(10 * (t + 1))
                continue
            raise
    return False


def run_wayback(con, limit=0, workers=3, tor=False):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    rows = pending_wayback(con, limit)
    con.execute("PRAGMA busy_timeout=120000")
    proxy = os.environ.get("ESG_PROXY") if tor else None
    pace = _Pace(float(os.environ.get(
        "ESG_WB_PACE", "0.6" if tor else "1.5")))
    ok = zero_run = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_wayback, r, pace, proxy=proxy): r
                for r in rows}
        for i, fut in enumerate(as_completed(futs), 1):
            rid, status, body = fut.result()
            # IA soft-bans aggressive IPs with conn-refused (0) or 429 —
            # AIMD pace adapts up; a run of 30 means hard backoff
            if status in (0, 429):
                zero_run += 1
                pace.bad()
                if zero_run >= 30:
                    print("wayback: IA throttling/refusing — "
                          "backing off 10min", flush=True)
                    time.sleep(600)
                    zero_run = 0
            else:
                zero_run = 0
                pace.ok()
            if status == 200:
                try:
                    path, sha = sha256_store_put(body)
                    _write_locked(
                        con,
                        """UPDATE reports SET sha256=?, path=?, fetched_at=?,
                           acquired_at=?, acquisition_method='web_archive'
                           WHERE id=?""",
                        (sha, path, now(), now(), rid))
                    ok += 1
                except ValueError:
                    pass  # capture wasn't a clean PDF — tor lane's job
            elif status == 404:
                # definitive "not archived" — tor lane keeps it, wayback
                # skips it in later batches
                _write_locked(
                    con,
                    "INSERT OR REPLACE INTO jobs(kind,status,payload) "
                    "VALUES(?,?,?)",
                    ("harvest", "wb_miss", f'{{"report_id":{rid}}}'))
            if i % 50 == 0:
                print(f"wayback: {ok} fetched, {i - ok} not-archived "
                      f"({i}/{len(rows)})", flush=True)
    return ok, i - ok if rows else 0


# --- Common Crawl lane -----------------------------------------------------
# Third source: the CC index maps URL -> WARC byte-range; data.commoncrawl.org
# is S3-hosted, so misses on Wayback still resolve at bulk speed here.
_CC_API = None


def _cc_index_api():
    global _CC_API
    if not _CC_API:
        status, body, rc, _ = curl_fetch(
            "https://index.commoncrawl.org/collinfo.json", timeout=30)
        if status == 200:
            try:
                _CC_API = json.loads(body)[0]["cdx-api"]
            except Exception:
                pass
    return _CC_API


def _cc_lookup(url):
    """Latest 200-status capture -> (filename, offset, length) on hit,
    None on a definitive index miss, "?" when the index itself failed
    (5xx/timeout) — callers must NOT record misses for "?"."""
    api = _cc_index_api()
    if not api:
        return "?"
    from urllib.parse import quote
    q = (f"{api}?url={quote(url, safe='')}&output=json"
         f"&filter=status:200&limit=1")
    status, body, rc, _ = curl_fetch(q, timeout=30)
    if status != 200:
        return "?"
    for ln in body.strip().split(b"\n"):
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if rec.get("mime") == "application/pdf" and all(
                k in rec for k in ("filename", "offset", "length")):
            return rec["filename"], int(rec["offset"]), int(rec["length"])
        if all(k in rec for k in ("filename", "offset", "length")):
            return rec["filename"], int(rec["offset"]), int(rec["length"])
    return None


def fetch_cc(r, pace):
    """Index query -> WARC range fetch -> PDF payload. A miss means CC
    never captured it (or as non-PDF) — caller records cc_miss."""
    time.sleep(pace.v)
    rec = _cc_lookup(r["url"])
    if rec == "?":
        return r["id"], 0, b""   # index outage — retryable, never a miss
    if not rec:
        return r["id"], 404, b""
    filename, offset, length = rec
    end = offset + length - 1
    cmd = ["curl", "-s", "-m", "120", "-r", f"{offset}-{end}",
           "-o", "-", f"https://data.commoncrawl.org/{filename}"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=150)
    except Exception:
        return r["id"], 0, b""
    if p.returncode != 0 or not p.stdout:
        return r["id"], 0, b""
    try:
        import io
        from warcio.archiveiterator import ArchiveIterator
        for wrec in ArchiveIterator(io.BytesIO(p.stdout)):
            if wrec.rec_type == "response":
                return r["id"], 200, wrec.content_stream().read()
    except Exception:
        pass
    return r["id"], 0, b""


def pending_cc(con, limit=0):
    """RR PDFs not in CC yet — wb_miss rows first (wayback will never serve
    those), then everything else pending, oldest-cataloged first."""
    miss = ("SELECT CAST(json_extract(payload,'$.report_id') AS INT) "
            "FROM jobs WHERE kind='harvest' AND status='{}'")
    q = f"""SELECT r.id, r.url FROM reports r
            WHERE r.sha256 IS NULL
              AND r.url LIKE '%responsibilityreports.com/HostedData/%.pdf'
              AND r.id NOT IN ({miss.format('cc_miss')})
            ORDER BY r.id IN ({miss.format('wb_miss')}) DESC, r.id ASC"""
    if limit:
        q += f" LIMIT {limit}"
    return con.execute(q).fetchall()


def run_commoncrawl(con, limit=0, workers=2):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    rows = pending_cc(con, limit)
    con.execute("PRAGMA busy_timeout=120000")
    pace = _Pace(1.0)
    ok = zero_run = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_cc, r, pace): r for r in rows}
        for i, fut in enumerate(as_completed(futs), 1):
            rid, status, body = fut.result()
            if status == 0:
                zero_run += 1
                pace.bad()
                if zero_run >= 30:
                    print("cc: index refusing — backing off 10min",
                          flush=True)
                    time.sleep(600)
                    zero_run = 0
            else:
                zero_run = 0
                pace.ok()
            if status == 200:
                try:
                    path, sha = sha256_store_put(body)
                    _write_locked(
                        con,
                        """UPDATE reports SET sha256=?, path=?, fetched_at=?,
                           acquired_at=?, acquisition_method='common_crawl'
                           WHERE id=?""",
                        (sha, path, now(), now(), rid))
                    ok += 1
                except ValueError:
                    pass
            elif status == 404:
                _write_locked(
                    con,
                    "INSERT OR REPLACE INTO jobs(kind,status,payload) "
                    "VALUES(?,?,?)",
                    ("harvest", "cc_miss", f'{{"report_id":{rid}}}'))
            if i % 50 == 0:
                print(f"cc: {ok} fetched, {i - ok} miss ({i}/{len(rows)})",
                      flush=True)
    return ok, i - ok if rows else 0


# --- Save Page Now lane ----------------------------------------------------
# For RR PDFs archived NOWHERE (wb_miss): ask IA to capture the URL itself —
# IA's crawler fetches RR, we later pull the fresh capture via replay, and
# RR's edge sees zero traffic from us. Anonymous SPN is tight (~1/30s and
# daily caps), so this trims the tail rather than replacing the Tor lane.
SPN = "https://web.archive.org/save/"


def pending_spn(con, limit=0):
    """wb_miss rows not yet SPN-requested twice. The wayback lane excludes
    wb_miss rows — after an SPN request lands we delete the marker so the
    wayback lane retries the row once the capture has propagated; a
    re-miss loops back here (bounded by tries<2)."""
    q = """SELECT r.id, r.url FROM reports r
           WHERE r.sha256 IS NULL
             AND r.url LIKE '%responsibilityreports.com/HostedData/%.pdf'
             AND r.id IN (
               SELECT CAST(json_extract(payload,'$.report_id') AS INT)
               FROM jobs WHERE kind='harvest' AND status='wb_miss')
             AND COALESCE((
               SELECT MAX(CAST(json_extract(s.payload,'$.tries') AS INT))
               FROM jobs s WHERE s.kind='harvest' AND s.status='spn_req'
                 AND CAST(json_extract(s.payload,'$.report_id') AS INT)=r.id
              ),0) < 2
           ORDER BY r.id DESC"""
    if limit:
        q += f" LIMIT {limit}"
    return con.execute(q).fetchall()


def run_spn(con, limit=0, pace_s=35):
    rows = pending_spn(con, limit or 150)
    con.execute("PRAGMA busy_timeout=120000")
    ok = 0
    for i, r in enumerate(rows, 1):
        tries = con.execute(
            """SELECT COALESCE(MAX(CAST(json_extract(payload,'$.tries')
               AS INT)),0) FROM jobs WHERE kind='harvest'
               AND status='spn_req'
               AND CAST(json_extract(payload,'$.report_id') AS INT)=?""",
            (r["id"],)).fetchone()[0]
        status, _, rc, _ = curl_fetch(SPN + r["url"], timeout=120)
        if status in (200, 301, 302) or status == 0 and rc == 28:
            # 200 = captured now; 3xx = capture queued; a curl timeout on
            # /save/ can still mean IA accepted the job server-side
            _write_locked(
                con,
                "INSERT INTO jobs(kind,status,payload) VALUES(?,?,?)",
                ("harvest", "spn_req",
                 f'{{"report_id":{r["id"]},"tries":{tries + 1}}}'))
            _write_locked(
                con,
                "DELETE FROM jobs WHERE kind='harvest' AND "
                "status='wb_miss' AND "
                "CAST(json_extract(payload,'$.report_id') AS INT)=?",
                (r["id"],))
            ok += 1
        elif status in (429, 403, 503):
            print(f"spn: refused ({status}) — backing off 15min",
                  flush=True)
            time.sleep(900)
        if i % 20 == 0:
            print(f"spn: {ok} requested ({i}/{len(rows)})", flush=True)
        time.sleep(pace_s)
    print(f"spn: {ok} capture requests ({len(rows)} candidates)",
          flush=True)
    return ok


# --- issuer-direct lane ----------------------------------------------------
# DataNeed/annualreports rows carry issuer-hosted URLs across thousands of
# independent domains — no shared throttle exists, so these fetch direct at
# full parallelism. The ONLY rate limit is per-domain, and each domain sees
# ~1 request per report lifetime.


def pending_issuer(con, limit=0):
    """Non-RR-hosted pending PDFs. Companies that still have RR-hosted rows
    pending go first (fills the existing backlog tail fastest), then
    breadth by report id."""
    q = """SELECT r.id, r.url FROM reports r
           WHERE r.sha256 IS NULL AND r.url LIKE '%.pdf'
             AND r.url NOT LIKE '%responsibilityreports%'
             AND r.id NOT IN (
               SELECT CAST(json_extract(payload,'$.report_id') AS INT)
               FROM jobs WHERE kind='harvest' AND status='issuer_miss')
           ORDER BY
             (r.company_id IN (
               SELECT DISTINCT company_id FROM reports
               WHERE sha256 IS NULL
                 AND url LIKE '%responsibilityreports%')) DESC,
             r.id"""
    if limit:
        q += f" LIMIT {limit}"
    return con.execute(q).fetchall()


def _commit_pdf(con, rid, body, method):
    """Validate + store + mark fetched, deduping on content sha: if the same
    bytes were already captured under another report row (RR copy vs issuer
    copy), drop this row instead — provenance stays with the first fetch."""
    path, sha = sha256_store_put(body)   # raises ValueError on non-PDF
    dup = con.execute(
        "SELECT id FROM reports WHERE sha256=? AND id<>?", (sha, rid)
        ).fetchone()
    if dup:
        con.execute("DELETE FROM reports WHERE id=?", (rid,))
        con.execute(
            "INSERT OR REPLACE INTO jobs(kind,status,payload) VALUES(?,?,?)",
            ("harvest", "dup_sha", f'{{"report_id":{rid},"same_as":{dup[0]}}}'))
        con.commit()
        return "dup"
    _write_locked(
        con,
        """UPDATE reports SET sha256=?, path=?, fetched_at=?,
           acquired_at=?, acquisition_method=? WHERE id=?""",
        (sha, path, now(), now(), method, rid))
    return "ok"


def fetch_issuer(r, retries=2):
    """Direct fetch of an issuer-hosted PDF. Returns (id, status, body);
    403/429 = host refuses our UA/ip — retryable later, not a hard miss."""
    for attempt in range(retries):
        status, body, rc, elapsed = curl_fetch(r["url"], proxy=None,
                                               timeout=120)
        if status == 200 and rc == 0:
            return r["id"], status, body
        if status in (404, 410):
            return r["id"], status, b""
        time.sleep(3 * (attempt + 1))
    return r["id"], status, b""


def run_issuer(con, limit=0, workers=20):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    rows = pending_issuer(con, limit)
    con.execute("PRAGMA busy_timeout=120000")
    ok = miss = dup = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_issuer, r): r for r in rows}
        for i, fut in enumerate(as_completed(futs), 1):
            rid, status, body = fut.result()
            if status == 200:
                try:
                    res = _commit_pdf(con, rid, body, "issuer_direct")
                    if res == "ok":
                        ok += 1
                    else:
                        dup += 1
                except ValueError:
                    _write_locked(
                        con,
                        "INSERT OR REPLACE INTO jobs(kind,status,payload) "
                        "VALUES(?,?,?)",
                        ("harvest", "issuer_fail",
                         f'{{"report_id":{rid},"status":{status}}}'))
            elif status in (404, 410):
                _write_locked(
                    con,
                    "INSERT OR REPLACE INTO jobs(kind,status,payload) "
                    "VALUES(?,?,?)",
                    ("harvest", "issuer_miss", f'{{"report_id":{rid}}}'))
                miss += 1
            else:
                _write_locked(
                    con,
                    "INSERT OR REPLACE INTO jobs(kind,status,payload) "
                    "VALUES(?,?,?)",
                    ("harvest", "issuer_fail",
                     f'{{"report_id":{rid},"status":{status}}}'))
            if i % 50 == 0:
                print(f"issuer: {ok} fetched, {miss} miss, {dup} dup, "
                      f"{i - ok - miss - dup} fail ({i}/{len(rows)})",
                      flush=True)
    return ok, miss


def run(con, limit=0, workers=4):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    proxy = os.environ.get("ESG_PROXY")
    rows = pending(con, limit)
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one, r, proxy): r for r in rows}
        for i, fut in enumerate(as_completed(futs), 1):
            rid, status, body = fut.result()
            r = futs[fut]
            if status == 200:
                try:
                    _commit_pdf(con, rid, body,
                                "tor_tunnel" if proxy else "http_get")
                    ok += 1
                except ValueError:
                    _write_locked(
                        con,
                        "INSERT OR REPLACE INTO jobs(kind,status,payload) "
                        "VALUES(?,?,?)",
                        ("harvest", "bad_pdf", f'{{"report_id":{rid}}}'))
            else:
                fail += 1
                _write_locked(
                    con,
                    "INSERT OR REPLACE INTO jobs(kind,status,payload) "
                    "VALUES(?,?,?)",
                    ("harvest", "failed",
                     f'{{"report_id":{rid},"status":{status}}}'))
            if i % 20 == 0:
                print(f"harvest: {ok} ok, {fail} failed ({i}/{len(rows)})")
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4,
                    help="concurrent curl lanes")
    ap.add_argument("--wayback", action="store_true",
                    help="fetch RR PDFs via Wayback Machine (no proxy)")
    ap.add_argument("--wayback-tor", action="store_true",
                    help="wayback fetches through Tor exits — per-exit "
                    "politeness with aggregate throughput; NEWNYM on "
                    "throttled/stalled circuits")
    ap.add_argument("--spn", action="store_true",
                    help="ask IA Save Page Now to capture wb_miss RR PDFs")
    ap.add_argument("--commoncrawl", action="store_true",
                    help="fetch RR PDFs via Common Crawl WARC (no proxy)")
    ap.add_argument("--issuer", action="store_true",
                    help="fetch non-RR-hosted PDFs (dataneed/annualreports) "
                    "direct from issuer domains — no shared throttle")
    a = ap.parse_args()
    con = db()
    if a.wayback or a.wayback_tor:
        ok, miss = run_wayback(con, a.limit, a.workers, tor=a.wayback_tor)
        print(f"wayback: {ok} fetched, {miss} not-archived")
    elif a.spn:
        run_spn(con, a.limit)
    elif a.issuer:
        ok, miss = run_issuer(con, a.limit, a.workers)
        print(f"issuer: {ok} fetched, {miss} miss")
    elif a.commoncrawl:
        ok, miss = run_commoncrawl(con, a.limit, a.workers)
        print(f"commoncrawl: {ok} fetched, {miss} miss")
    else:
        ok, fail = run(con, a.limit, a.workers)
        print(f"harvest: {ok} fetched, {fail} failed")


if __name__ == "__main__":
    main()
