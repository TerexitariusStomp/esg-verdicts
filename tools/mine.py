#!/usr/bin/env python3
"""mine.py — process-the-corpus-only supervisor. NO harvesting: grinds the
banked PDFs through pages -> figures -> extract (det+LLM) -> downstream
stages, then exits. Desktop-friendly: lanes are pinned off the desktop
island (cores 16-23), niced, cgroup memory caps. The pin pool is ADAPTIVE —
wide (0-15) while the desktop cores sit idle, narrow (0-7) under load;
running lanes are live-repinned via taskset -pc, no relaunch needed.

Usage:  nohup .venv/bin/python tools/mine.py > log/mine.log 2>&1 &
Stop:   touch STOP_MINE
"""
import json
import os
import sqlite3
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv/bin/python")
LOGDIR = os.path.join(ROOT, "log")
DB = os.path.join(ROOT, "data/esg.db")
STOP = os.path.join(ROOT, "STOP_MINE")

# pool -> (wide cores, narrow cores). figures/extract stay off llama's
# cores in wide mode so prompt eval doesn't starve decode feeding.
POOLS = {
    "figures": ("0-5", "0-3"),
    "extract": ("6-11", "4-7"),
    "llama": ("12-15", "0-7"),
    "misc": ("0-15", "0-7"),
}
WIDE = False  # start conservative; adapt() widens after 2 idle samples

# pgrep pattern -> pool. Patterns carry the shard modulus so a stale
# N=2 lane can never masquerade as a live N=4 lane.
LANE_POOLS = [
    (r"esg\.figures --shard", "figures"),
    (r"esg\.extract --shard", "extract"),
    (r"esg\.extract --drain-llm", "extract"),
    (r"esg\.pages", "misc"),
    (r"esg\.compact", "misc"),
    (r"llama-server", "llama"),
]

# onnxruntime/BLAS otherwise spawn nproc threads per lane and thrash the
# shared pool (observed: load 109 on 8 pinned cores).
LANE_ENV = {"OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1"}

# downstream stages run sequentially once extraction queues drain
STAGES = [
    (["-m", "esg.claims", "--workers", "4"], "mine-claims.log"),
    (["-m", "esg.geo", "--matched"], "mine-geo.log"),
    (["-m", "esg.litigate"], "mine-litigate.log"),
    (["-m", "esg.filings"], "mine-filings.log"),
    (["-m", "esg.flags"], "mine-flags.log"),
    (["-m", "esg.greenwash"], "mine-greenwash.log"),
    (["-m", "esg.evaluate"], "mine-evaluate.log"),
    (["-m", "esg.verify"], "mine-verify.log"),
    (["-m", "esg.evidence"], "mine-evidence.log"),
    (["-m", "esg.export"], "mine-export.log"),
]


def running(pat):
    try:
        out = subprocess.run(["pgrep", "-f", pat],
                             capture_output=True, text=True).stdout.strip()
        return bool(out)
    except Exception:
        return True


def bg(args, log, capped_mem=None, pool="misc", gpu=False):
    cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
    if capped_mem:
        cmd += ["-p", f"MemoryMax={capped_mem}"]
    cmd += ["-p", "CPUWeight=20", "taskset", "-c",
            POOLS[pool][0 if WIDE else 1], "nice", "-n", "15"] + args
    env = dict(os.environ, **LANE_ENV)
    if not gpu:
        env["CUDA_VISIBLE_DEVICES"] = ""
    with open(os.path.join(LOGDIR, log), "ab") as f:
        return subprocess.Popen(cmd, cwd=ROOT, env=env,
                                stdout=f, stderr=subprocess.STDOUT)


def seq(args, log, timeout=7200):
    with open(os.path.join(LOGDIR, log), "ab") as f:
        try:
            subprocess.run([PY] + args, cwd=ROOT,
                           env=dict(os.environ, CUDA_VISIBLE_DEVICES="",
                                    **LANE_ENV),
                           stdout=f, stderr=subprocess.STDOUT,
                           timeout=timeout)
        except subprocess.TimeoutExpired:
            pass


def _mem_avail_gb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    return int(line.split()[1]) / 1e6
    except OSError:
        pass
    return 0.0


def _cores_busy(cores, dt=0.4):
    """Mean busy fraction over the given cores, sampled over dt seconds."""
    def snap():
        vals = {}
        with open("/proc/stat") as f:
            for line in f:
                p = line.split()
                if len(p) > 4 and p[0].startswith("cpu") \
                        and p[0][3:].isdigit() and int(p[0][3:]) in cores:
                    t = [int(x) for x in p[1:8]]
                    vals[int(p[0][3:])] = (sum(t), t[3] + t[4])
        return vals
    try:
        a = snap()
        time.sleep(dt)
        b = snap()
    except OSError:
        return 1.0
    busy = [1 - (b[c][1] - a[c][1]) / (b[c][0] - a[c][0])
            for c in cores if c in a and c in b and b[c][0] > a[c][0]]
    return sum(busy) / len(busy) if busy else 1.0


def _repin():
    for pat, pool in LANE_POOLS:
        cores = POOLS[pool][0 if WIDE else 1]
        out = subprocess.run(["pgrep", "-f", pat], capture_output=True,
                             text=True).stdout.split()
        for pid in out:
            subprocess.run(["taskset", "-pc", cores, pid],
                           capture_output=True)


_consec = 0


def _adapt():
    """Widen the pool when the cores lanes would take over are idle; in
    narrow mode watch 8-23 (all non-pipeline), in wide watch the desktop
    island 16-23. Two consecutive agreeing samples before switching."""
    global WIDE, _consec
    want = _cores_busy(range(16, 24) if WIDE else range(8, 24)) < 0.35 \
        and _mem_avail_gb() >= 4
    if want == WIDE:
        _consec = 0
        return
    _consec += 1
    if _consec >= 2:
        WIDE = want
        _consec = 0
        _repin()
        print(f"[mine] pool -> {'wide 0-15' if WIDE else 'narrow 0-7'}",
              flush=True)


def pending(kind, statuses):
    ph = ",".join("?" * len(statuses))
    try:
        con = sqlite3.connect(DB)
        n = con.execute(
            f"SELECT COUNT(*) FROM jobs WHERE kind=? AND status IN ({ph})",
            (kind, *statuses)).fetchone()[0]
        con.close()
        return n
    except Exception:
        return 1


def pages_left():
    try:
        con = sqlite3.connect(DB)
        n = con.execute(
            "SELECT COUNT(*) FROM reports WHERE sha256 IS NOT NULL AND "
            "path IS NOT NULL AND (pages IS NULL OR pages=0)").fetchone()[0]
        con.close()
        return n
    except Exception:
        return 1


def main():
    os.chdir(ROOT)
    it = 0
    while not os.path.exists(STOP):
        it += 1
        _adapt()
        # llama-server for the LLM tier
        if not running("llama-server"):
            subprocess.Popen(
                ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                 "-p", "MemoryMax=8G", "-p", "CPUWeight=40",
                 "taskset", "-c", POOLS["llama"][0 if WIDE else 1],
                 os.path.expanduser(
                     "~/llama.cpp/build/bin/llama-server"),
                 "-m", os.path.expanduser(
                     "~/models/Llama-3.2-3B-Instruct-Q4_K_M.gguf"),
                 "-md", os.path.expanduser(
                     "~/models/Llama-3.2-1B-Instruct-Q4_K_M.gguf"),
                 "--spec-type", "draft-simple", "--port", "8080",
                 "-ngl", "99", "-c", "65536", "-np", "8",
                 "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
                 "-fa", "on", "--jinja"],
                stdout=open(os.path.join(LOGDIR, "mine-llama.log"), "ab"),
                stderr=subprocess.STDOUT, cwd=ROOT)
            time.sleep(20)
        # pages: single lane — few reports left
        if pages_left() > 0 and not running("esg.pages"):
            bg([PY, "-m", "esg.pages"], "mine-pages.log", "4G")
        # figures: 2 shards on the GPU (RapidOCR CUDA ~20x CPU)
        for k in range(2):
            if not running(rf"esg\.figures --shard {k} 2"):
                bg([PY, "-m", "esg.figures", "--shard", str(k), "2"],
                   f"mine-figures{k}.log", "4G", pool="figures", gpu=True)
        # extract: 4 deterministic shards on the extract pool
        for k in range(4):
            if not running(rf"esg\.extract --shard {k} 4"):
                bg([PY, "-m", "esg.extract", "--shard", str(k), "4",
                    "--llm-workers", "2", "--batch", "6", "--no-docling"],
                   f"mine-extract{k}.log", "5G", pool="extract")
        # LLM drain: 8 workers fill every llama-server slot (-np 8);
        # only while it has work
        if pending("extract", ["pending_llm"]) > 0 and \
                not running(r"esg\.extract --drain-llm"):
            bg([PY, "-m", "esg.extract", "--drain-llm",
                "--llm-workers", "8", "--batch", "6", "--no-docling"],
               "mine-extract-llm.log", "4G", pool="extract")
        # compact already-processed raw PDFs to keep disk sane
        if not running("esg.compact"):
            bg([PY, "-m", "esg.compact", "1000"], "mine-compact.log", "1G")
        det = pending("extract", ["pending"])
        llm = pending("extract", ["pending_llm"])
        fig = pending("figures", ["pending"])
        pg = pages_left()
        print(f"[mine {it}] pool={'wide' if WIDE else 'narrow'} "
              f"pages={pg} figures={fig} det={det} llm={llm}", flush=True)
        if det == 0 and llm == 0 and fig == 0 and pg == 0:
            break
        time.sleep(60)
    if os.path.exists(STOP):
        print("STOP_MINE found — exiting", flush=True)
        return
    # downstream stages, sequentially — mining is done
    for args, log in STAGES:
        if os.path.exists(STOP):
            break
        print(f"[mine] stage {args}", flush=True)
        seq(args, log)
    print("[mine] all stages complete", flush=True)


if __name__ == "__main__":
    main()
