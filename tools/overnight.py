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
import shutil
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
            "--port", "8080", "-ngl", "99",
            # 8k ctx/slot: batch=6 page prompts (~5-6k tok) need it —
            # 24576/8=3072 was silently rejecting every batch call.
            # q8 KV cache halves the ~7.4GB fp16 KV to fit the 12GB card.
            "-c", "65536", "-np", "8",
            "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
            # this build treats bare -fa as a parse error — needs 'on'
            # explicitly, before --jinja
            "-fa", "on", "--jinja"], "llama.log")
        time.sleep(15)


def pending_pdfs():
    try:
        con = sqlite3.connect(DB)
        return con.execute(
            "SELECT COUNT(*) FROM reports WHERE path IS NULL "
            "AND url LIKE '%.pdf'").fetchone()[0]
    except sqlite3.Error:
        return 9999


def uncrawled_slugs(source="responsibilityreports"):
    """Slugs not yet recorded as catalog-done — the coverage frontier.
    source filters both slug_map and the done-marker payloads (AR done
    jobs carry "source":"annualreports"; legacy RR jobs carry no source)."""
    try:
        con = sqlite3.connect(DB)
        total = con.execute(
            "SELECT COUNT(*) FROM slug_map WHERE source=?",
            (source,)).fetchone()[0]
        done = len({json.loads(r[0]).get("slug") for r in con.execute(
            "SELECT payload FROM jobs WHERE kind='catalog' "
            "AND status='done'")
            if json.loads(r[0]).get(
                "source", "responsibilityreports") == source})
        return max(0, total - done)
    except Exception:
        return 1


def _one_shot_done(kind):
    try:
        con = sqlite3.connect(DB)
        return con.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind=? AND status='done'",
            (kind,)).fetchone()[0] > 0
    except Exception:
        return True  # err toward not re-running a heavy one-shot


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
    # disk watchdog: full history ~325GB vs ~390GB free — pause the fetch
    # lanes (not extract) when space gets tight so the box never wedges
    disk_low = shutil.disk_usage(ROOT).free < 30 * 1024**3
    if disk_low:
        print(f"[{time.strftime('%H:%M')}] DISK LOW (<30G) — "
              "harvest lanes paused this iter", flush=True)
    # "m esg.harvest --workers" won't match the --wayback lane's cmdline.
    # 16 tor workers: each request rides a different exit, so the same
    # per-IP politeness holds while aggregate throughput doubles.
    if not disk_low and not running("m esg.harvest --workers 16"):
        # bounded batch: process exits when drained, relaunch re-reads the
        # catalog so newly-crawled reports join the breadth-first queue
        bg([PY, "-m", "esg.harvest", "--workers", "16", "--limit", "1200"],
            "harvest.log")
    # Wayback direct: ~72% of pending RR PDFs are archived; measured
    # ~500/hr sustained direct vs ~17/hr through tor exits (tor bandwidth
    # is the bottleneck, not IA). AIMD pace + breaker keep it polite;
    # --wayback-tor remains in the code as an emergency fallback.
    if not disk_low and not running("esg.harvest .*wayback"):
        bg([PY, "-m", "esg.harvest", "--wayback", "--workers", "4",
            "--limit", "6000"], "wayback.log")
    # Common Crawl lane: covers the wb_miss tail — index query then S3-speed
    # WARC range fetch, no proxy needed
    if not disk_low and not running("esg.harvest .*commoncrawl"):
        bg([PY, "-m", "esg.harvest", "--commoncrawl", "--workers", "2",
            "--limit", "3000"], "commoncrawl.log")
    # SPN lane: for PDFs archived nowhere, ask IA to capture the URL itself
    # (IA fetches RR, we harvest the fresh capture via the wayback lane —
    # RR sees zero load from us). Anonymous SPN is rate-tight, pace ~1/35s.
    if not disk_low and not running("esg.harvest .*--spn"):
        bg([PY, "-m", "esg.harvest", "--spn", "--limit", "150"], "spn.log")
    # issuer-direct lane: DataNeed/annualreports rows fetch straight from
    # issuer domains — thousands of independent hosts, no shared throttle.
    # Ordered RR-backlog-companies-first so it fills the existing tail.
    if not disk_low and not running("esg.harvest .*--issuer"):
        bg([PY, "-m", "esg.harvest", "--issuer", "--workers", "20",
            "--limit", "5000"], "issuer.log")
    # one-shot catalog expansions: DataNeed download+ingest (~2GB JSON,
    # ~350k issuer URLs), then annualreports /Companies index + stubs
    if not _one_shot_done("dataneed") and not running("esg.dataneed"):
        seq(["-m", "esg.dataneed", "download"], "dataneed.log")
        seq(["-m", "esg.dataneed", "ingest"], "dataneed.log")
    try:
        _con = sqlite3.connect(DB)
        _ar_indexed = _con.execute(
            "SELECT COUNT(*) FROM slug_map WHERE source='annualreports'"
            ).fetchone()[0] > 0
        _con.close()
    except Exception:
        _ar_indexed = True
    if not _ar_indexed and not running("esg.catalog"):
        seq(["-m", "esg.catalog", "ar-index"], "ar-index.log")
        seq(["-m", "esg.catalog", "ar-stubify"], "ar-stubify.log")
    elif uncrawled_slugs("annualreports") > 0:
        # 4 shards through tor — each scrapy process rides separate exits,
        # so per-IP politeness holds while aggregate throughput doubles
        for m in range(4):
            if not running(f"catalog ar-crawl --shard 4 {m}"):
                bg([PY, "-u", "-m", "esg.catalog", "ar-crawl",
                    "--shard", "4", str(m)], f"ar-catalog{m}.log")
    # pages/figures as sharded lanes — single sequential stages can't keep
    # up with multi-k/hr PDF inflow; report_id % N keeps them disjoint
    for k in range(3):
        if not running(f"esg.pages --shard {k}"):
            bg([PY, "-m", "esg.pages", "--shard", str(k), "3"],
               f"pages{k}.log")
    for k in range(2):
        if not running(f"esg.figures --shard {k}"):
            bg([PY, "-m", "esg.figures", "--shard", str(k), "2"],
               f"figures{k}.log")
    # extract as 3 disjoint job-shard lanes (report_id % 3) — pass-1
    # parallelizes ~3x; CPUWeight=30 keeps the desktop responsive
    for k, llm in ((0, "4"), (1, "2"), (2, "2")):
        if not running(f"esg.extract --shard {k}"):
            bg([PY, "-m", "esg.extract", "--shard", str(k), "3",
                "--llm-workers", llm, "--batch", "6", "--no-docling"],
               f"extract{k}.log")
    # compact sweeper: once a report's pages/figures/extract pending work
    # is done the raw PDF is dead weight — unlink it (sha256 stays, so no
    # refetch; provenance keeps url+method). Turns disk from a hard cap
    # into a circular buffer; bounded 2000/iter so it never hogs the loop.
    if not running("esg.compact"):
        bg([PY, "-m", "esg.compact", "2000"], "compact.log")
    # dedicated LLM drain lane: pass-2 ran only AFTER a full pass-1 sweep,
    # leaving the GPU idle for hours. This lane consumes pending_llm
    # continuously (page-text dedup cache cuts call volume ~2x+).
    # drain-llm ordered newest-report-first (--llm-order desc in extract)
    if not running("esg.extract .*drain-llm"):
        bg([PY, "-m", "esg.extract", "--drain-llm",
            "--llm-workers", "8", "--batch", "6", "--no-docling"],
           "extract-llm.log")
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
