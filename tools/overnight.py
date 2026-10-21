#!/usr/bin/env python3
"""Overnight pipeline supervisor — keeps every stage fed until STOP file.

Design: network lanes (catalog crawl, harvest) run as background daemons and
self-relaunch; CPU/GPU/DB-heavy stages run strictly sequentially (SQLite is
single-writer). Creates a STOP file or kill the process to end cleanly —
everything is resumable/idempotent.

  nohup .venv/bin/python tools/overnight.py > log/overnight.log 2>&1 &
"""
import os
import sqlite3
import subprocess
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv/bin/python")
DB = os.path.join(ROOT, "data/esg.db")
LOGDIR = os.path.join(ROOT, "log")
STOP = os.path.join(ROOT, "STOP")
ENV = dict(os.environ, ESG_PROXY="http://127.0.0.1:9080",
           CLOUDFLARE_ACCOUNT_ID="6c0140469651854b2e358517371a03d3")
os.makedirs(LOGDIR, exist_ok=True)


def running(pat):
    return subprocess.run(["pgrep", "-f", pat],
                          capture_output=True).returncode == 0


def bg(args, log):
    with open(os.path.join(LOGDIR, log), "ab") as f:
        return subprocess.Popen(args, cwd=ROOT, env=ENV,
                                stdout=f, stderr=subprocess.STDOUT)


def seq(args, log, timeout=7200):
    with open(os.path.join(LOGDIR, log), "ab") as f:
        try:
            subprocess.run([PY] + args, cwd=ROOT, env=ENV,
                           stdout=f, stderr=subprocess.STDOUT,
                           timeout=timeout)
        except subprocess.TimeoutExpired:
            pass


def gw_alive():
    try:
        urllib.request.urlopen("http://127.0.0.1:8080/health",
                               timeout=3)
        return True
    except Exception:
        return False


def keepalive():
    if not running("tor -f"):
        bg(["tor", "-f", os.path.expanduser("~/tor-data/torrc")], "tor.log")
    if not gw_alive():
        bg([os.path.expanduser("~/llama.cpp/build/bin/llama-server"),
            "-m", os.path.expanduser(
                "~/models/Llama-3.2-3B-Instruct-Q4_K_M.gguf"),
            "-md", os.path.expanduser(
                "~/models/Llama-3.2-1B-Instruct-Q4_K_M.gguf"),
            "--spec-type", "draft-simple",
            "--port", "8080", "-ngl", "99", "-c", "24576",
            "-np", "8", "--jinja"], "llama.log")
        time.sleep(15)


def pending_pdfs():
    try:
        con = sqlite3.connect(DB)
        return con.execute(
            "SELECT COUNT(*) FROM reports WHERE path IS NULL "
            "AND url LIKE '%.pdf'").fetchone()[0]
    except sqlite3.Error:
        return 9999


def snapshot(tag):
    try:
        con = sqlite3.connect(DB)
        q = lambda s: con.execute(s).fetchone()[0]
        print(f"[{time.strftime('%H:%M')}] {tag}: "
              f"reports={q('select count(*) from reports')}, "
              f"pdf={q('select count(*) from reports where path is not null')}, "
              f"extract={q('select count(*) from extractions')}, "
              f"claims={q('select count(*) from claims')}, "
              f"flags={q('select count(*) from flags')}", flush=True)
    except sqlite3.Error as e:
        print(f"[{time.strftime('%H:%M')}] snapshot failed: {e}", flush=True)


STAGES = [
    (["-m", "esg.pages"], "pages.log"),
    (["-m", "esg.figures"], "figures.log"),
    (["-m", "esg.extract", "--llm-workers", "8", "--batch", "3"],
     "extract.log"),
    (["-m", "esg.filings", "--limit", "20"], "filings.log"),
    (["-m", "esg.litigate"], "litigate.log"),
    (["-m", "esg.verify"], "verify.log"),
    (["-m", "esg.flags"], "flags.log"),
    (["-m", "esg.export"], "export.log"),
]

print(f"[{time.strftime('%H:%M')}] overnight supervisor start", flush=True)
it = 0
while not os.path.exists(STOP):
    it += 1
    keepalive()
    if not running("m esg.harvest"):
        bg([PY, "-m", "esg.harvest", "--workers", "6"], "harvest.log")
    if not running("m esg.claims"):
        bg([PY, "-u", "-m", "esg.claims", "--workers", "8"], "claims.log")
    # expand catalog only when the harvest queue is draining
    if pending_pdfs() < 300 and not running("m esg.catalog"):
        bg([PY, "-u", "-m", "esg.catalog", "crawl", "--limit", "60"],
           "catalog.log")
    snapshot(f"iter {it}")
    for args, log in STAGES:
        if os.path.exists(STOP):
            break
        seq(args, log)
    time.sleep(180)
print("STOP file found — supervisor exiting", flush=True)
