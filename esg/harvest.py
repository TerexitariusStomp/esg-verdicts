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
    q = """SELECT r.id, r.url FROM reports r
           LEFT JOIN companies c ON c.id = r.company_id
           WHERE r.sha256 IS NULL AND r.url LIKE '%.pdf'
           ORDER BY
             (SELECT COUNT(*) FROM reports r2
              WHERE r2.company_id IS r.company_id AND r2.url LIKE '%.pdf'
                AND (r2.year > r.year
                     OR (r2.year = r.year AND r2.id < r.id))) ASC,
             COALESCE(c.priority,0) DESC, r.id"""
    if limit:
        q += f" LIMIT {limit}"
    return con.execute(q).fetchall()


def fetch_one(r, proxy, retries=4):
    """One report: retries with NEWNYM rotation on 403 or a stalled/
    timed-out circuit (a dying exit can crawl at ~10KB/s and every lane
    shares it). Returns (report_id, status, body)."""
    for attempt in range(retries):
        status, body, rc, elapsed = curl_fetch(r["url"], proxy)
        if status == 200 and rc == 0:
            return r["id"], status, body
        if proxy and (status == 403 or rc == 28 or elapsed > 90):
            _nym()
        else:
            time.sleep(min(20, 2 ** attempt * 2))
    return r["id"], status, b""


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
                    path, sha = sha256_store_put(body)
                    con.execute(
                        """UPDATE reports SET sha256=?, path=?, fetched_at=?,
                           acquired_at=?, acquisition_method=? WHERE id=?""",
                        (sha, path, now(), now(),
                         "tor_tunnel" if proxy else "http_get", rid))
                    ok += 1
                except ValueError:
                    con.execute(
                        "INSERT OR REPLACE INTO jobs(kind,status,payload) "
                        "VALUES(?,?,?)",
                        ("harvest", "bad_pdf", f'{{"report_id":{rid}}}'))
            else:
                fail += 1
                con.execute(
                    "INSERT OR REPLACE INTO jobs(kind,status,payload) "
                    "VALUES(?,?,?)",
                    ("harvest", "failed",
                     f'{{"report_id":{rid},"status":{status}}}'))
            con.commit()
            if i % 20 == 0:
                print(f"harvest: {ok} ok, {fail} failed ({i}/{len(rows)})")
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4,
                    help="concurrent curl lanes")
    a = ap.parse_args()
    con = db()
    ok, fail = run(con, a.limit, a.workers)
    print(f"harvest: {ok} fetched, {fail} failed")


if __name__ == "__main__":
    main()
