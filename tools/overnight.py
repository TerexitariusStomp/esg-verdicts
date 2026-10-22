#!/usr/bin/env python3
"""Overnight pipeline supervisor — keeps every stage fed until STOP file.

Design: network lanes (catalog crawl, harvest) run as background daemons and
self-relaunch; CPU/GPU/DB-heavy stages run strictly sequentially (SQLite is
single-writer). Creates a STOP file or kill the process to end cleanly —
everything is resumable/idempotent.

  nohup .venv/bin/python tools/overnight.py > log/overnight.log 2>&1 &
"""
import json
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


# per-lane cgroup memory caps (30GB box): a runaway stage is OOM-killed and
# relaunched next iteration — the machine never freezes
MEM_CAPS = [
    ("esg.extract", "6G"),   # docling+torch+pdfplumber (x3 shards)
    ("esg.figures", "6G"),   # onnxruntime-gpu
    ("esg.claims", "6G"),
    ("esg.pages", "4G"),
    ("esg.export", "4G"),
    ("esg.geo", "3G"),
    ("esg.catalog", "3G"),
    ("esg.harvest", "2G"),
    ("llama-server", "8G"),
]
MEM_DEFAULT = "3G"

# stages that build+teardown CUDA contexts every iteration — run CPU-side
# to avoid NVRM unmap driver crashes (llama-server keeps the GPU)
NO_GPU_STAGES = {"esg.figures", "esg.extract"}


def capped(args):
    mem = MEM_DEFAULT
    for pat, m in MEM_CAPS:
        if any(pat in a for a in args):
            mem = m
            break
    out = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
           "-p", f"MemoryMax={mem}"]
    # extract shards + figures are multi-threaded CPU (docling/onnx spawn
    # ~117 threads/process regardless of OMP_* envs) — taskset-pins them to
    # cores 0-15 so cores 16-23 always stay free for the desktop. (cgroup
    # AllowedCPUs needs cpuset delegation user scopes lack; taskset doesn't)
    if any("esg.extract" in a or "esg.figures" in a for a in args):
        return out + ["-p", "CPUWeight=30",
                      "--setenv=OMP_NUM_THREADS=6",
                      "--setenv=TORCH_NUM_THREADS=6",
                      "taskset", "-c", "0-15"] + args
    return out + args


def mem_ok():
    """Only start heavy stages when >3GB is available."""
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable"):
                return int(line.split()[1]) > 3 * 1024 * 1024
    except OSError:
        pass
    return True


def bg(args, log):
    env = ENV
    if any(s in args for s in NO_GPU_STAGES):
        # same CUDA-teardown guard as seq(): lanes must not build GPU
        # contexts — llama-server owns the GPU
        env = dict(ENV, CUDA_VISIBLE_DEVICES="")
    with open(os.path.join(LOGDIR, log), "ab") as f:
        return subprocess.Popen(capped(args), cwd=ROOT, env=env,
                                stdout=f, stderr=subprocess.STDOUT)


def seq(args, log, timeout=7200):
    env = ENV
    if any(s in args for s in NO_GPU_STAGES):
        # per-iteration CUDA context churn trips NVRM unmap asserts on
        # driver 610 — keep GPU for the long-lived llama-server only
        env = dict(ENV, CUDA_VISIBLE_DEVICES="")
    with open(os.path.join(LOGDIR, log), "ab") as f:
        try:
            subprocess.run(capped([PY] + args), cwd=ROOT, env=env,
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


def uncrawled_slugs():
    """RR slugs not yet recorded as catalog-done — the coverage frontier."""
    try:
        con = sqlite3.connect(DB)
        total = con.execute(
            "SELECT COUNT(*) FROM slug_map "
            "WHERE source='responsibilityreports'").fetchone()[0]
        done = len({json.loads(r[0]).get("slug") for r in con.execute(
            "SELECT payload FROM jobs WHERE kind='catalog' "
            "AND status='done'")})
        return max(0, total - done)
    except Exception:
        return 1


def snapshot(tag):
    try:
        con = sqlite3.connect(DB)
        q = lambda s: con.execute(s).fetchone()[0]
        print(f"[{time.strftime('%H:%M')}] {tag}: "
              f"companies={q('select count(distinct company_id) from reports')}"
              f"/{q('select count(*) from companies')}, "
              f"cos_with_pdf={q('select count(distinct company_id) from reports where path is not null')}, "
              f"cos_with_xtract={q('select count(distinct company_id) from extractions e join reports r on r.id=e.report_id')}, "
              f"reports={q('select count(*) from reports')}, "
              f"pdf={q('select count(*) from reports where path is not null')}, "
              f"extract={q('select count(*) from extractions')}, "
              f"claims={q('select count(*) from claims')}, "
              f"flags={q('select count(*) from flags')}, "
              f"uncrawled={uncrawled_slugs()}", flush=True)
    except sqlite3.Error as e:
        print(f"[{time.strftime('%H:%M')}] snapshot failed: {e}", flush=True)


STAGES = [
    (["-m", "esg.pages"], "pages.log"),
    (["-m", "esg.figures"], "figures.log"),
    (["-m", "esg.filings", "--limit", "20"], "filings.log"),
    (["-m", "esg.litigate"], "litigate.log"),
    (["-m", "esg.verify"], "verify.log"),
    (["-m", "esg.flags"], "flags.log"),
    (["-m", "esg.export"], "export.log"),
    # sales funnel: packs for severity-ordered flagged companies,
    # then mirror publishable rows to D1 when ESG_SYNC_D1=1
    (["-m", "esg.evidence", "--top", "20"], "packs.log"),
    (["tools/sync_public.py"], "sync_public.log"),
]

print(f"[{time.strftime('%H:%M')}] overnight supervisor start", flush=True)
it = 0
while not os.path.exists(STOP):
    it += 1
    keepalive()
    # "m esg.harvest --workers" won't match the --wayback lane's cmdline
    if not running("m esg.harvest --workers 8"):
        # bounded batch: process exits when drained, relaunch re-reads the
        # catalog so newly-crawled reports join the breadth-first queue
        bg([PY, "-m", "esg.harvest", "--workers", "8", "--limit", "800"],
            "harvest.log")
    # Wayback lane: ~72% of pending RR PDFs are archived; IA serves them
    # at multi-MB/s with no proxy and zero load on RR's edge. Big bound —
    # the batch spans iterations so the lane never idles mid-run. AIMD
    # pacing (ESG_WB_PACE) self-tunes to IA's tolerated rate.
    if not running("esg.harvest .*wayback"):
        bg([PY, "-m", "esg.harvest", "--wayback", "--workers", "3",
            "--limit", "5000"], "wayback.log")
    # Common Crawl lane: covers the wb_miss tail — index query then S3-speed
    # WARC range fetch, no proxy needed
    if not running("esg.harvest .*commoncrawl"):
        bg([PY, "-m", "esg.harvest", "--commoncrawl", "--workers", "2",
            "--limit", "3000"], "commoncrawl.log")
    # extract as 3 disjoint job-shard lanes (id % 3) — pass-1 parallelizes
    # ~3x; LLM workers sum to llama-server's -np 8 slots; CPUWeight=30 keeps
    # the desktop responsive under load
    for k, llm in ((0, "4"), (1, "2"), (2, "2")):
        if not running(f"esg.extract --shard {k}"):
            bg([PY, "-m", "esg.extract", "--shard", str(k), "3",
                "--llm-workers", llm, "--batch", "6"],
               f"extract{k}.log")
    if not running("m esg.claims"):
        bg([PY, "-u", "-m", "esg.claims", "--workers", "8"], "claims.log")
    # geo is network-bound (Climate TRACE API), small per-company commits —
    # runs as a continuous lane, not a sequential stage; resumable via
    # jobs markers, exits when every matched company is enriched
    if not running("m esg.geo"):
        bg([PY, "-u", "-m", "esg.geo", "--matched"], "geo.log")
    # catalog shards run until every RR slug is crawled — breadth coverage
    # IS the goal; they exit cleanly once slug_map is exhausted
    if uncrawled_slugs() > 0:
        for m in range(2):
            if not running(f"catalog crawl --shard 2 {m}"):
                bg([PY, "-u", "-m", "esg.catalog", "crawl",
                    "--shard", "2", str(m)], f"catalog{m}.log")
    snapshot(f"iter {it}")
    for args, log in STAGES:
        if os.path.exists(STOP):
            break
        # supervisor restarts orphan stage children — never double-run one
        if running(" ".join(a for a in args if "esg." in a or a.endswith(".py"))):
            continue
        if not mem_ok():
            print(f"[{time.strftime('%H:%M')}] low memory — "
                  "skipping remaining stages this iter", flush=True)
            break
        seq(args, log)
    time.sleep(90)
print("STOP file found — supervisor exiting", flush=True)
