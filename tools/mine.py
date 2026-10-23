#!/usr/bin/env python3
"""mine.py — process-the-corpus-only supervisor. NO harvesting: grinds the
banked PDFs through pages -> figures -> extract (det+LLM) -> downstream
stages, then exits. Made to run alongside a usable desktop: all CPU lanes
pinned to cores 0-7 via taskset, niced, cgroup memory caps.

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
PIN = ["taskset", "-c", "0-7", "nice", "-n", "15"]

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


def bg(args, log, capped_mem=None):
    cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
    if capped_mem:
        cmd += ["-p", f"MemoryMax={capped_mem}"]
    cmd += ["-p", "CPUWeight=20"] + PIN + args
    with open(os.path.join(LOGDIR, log), "ab") as f:
        return subprocess.Popen(cmd, cwd=ROOT,
                                env=dict(os.environ, CUDA_VISIBLE_DEVICES=""),
                                stdout=f, stderr=subprocess.STDOUT)


def seq(args, log, timeout=7200):
    with open(os.path.join(LOGDIR, log), "ab") as f:
        try:
            subprocess.run([PY] + args, cwd=ROOT,
                           env=dict(os.environ, CUDA_VISIBLE_DEVICES=""),
                           stdout=f, stderr=subprocess.STDOUT,
                           timeout=timeout)
        except subprocess.TimeoutExpired:
            pass


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
        # llama-server for the LLM tier
        if not running("llama-server"):
            subprocess.Popen(
                ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                 "-p", "MemoryMax=8G", "-p", "CPUWeight=40",
                 "taskset", "-c", "0-7", os.path.expanduser(
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
        # figures: 2 shards
        for k in range(2):
            if not running(f"esg.figures --shard {k}"):
                bg([PY, "-m", "esg.figures", "--shard", str(k), "2"],
                   f"mine-figures{k}.log", "4G")
        # extract: 2 deterministic shards on the reduced pool
        for k in range(2):
            if not running(f"esg.extract --shard {k}"):
                bg([PY, "-m", "esg.extract", "--shard", str(k), "2",
                    "--llm-workers", "2", "--batch", "6", "--no-docling"],
                   f"mine-extract{k}.log", "5G")
        # LLM drain: only while it has work
        if pending("extract", ["pending_llm"]) > 0 and \
                not running("esg.extract .*drain-llm"):
            bg([PY, "-m", "esg.extract", "--drain-llm",
                "--llm-workers", "4", "--batch", "6", "--no-docling"],
               "mine-extract-llm.log", "4G")
        # compact already-processed raw PDFs to keep disk sane
        if not running("esg.compact"):
            bg([PY, "-m", "esg.compact", "1000"], "mine-compact.log", "1G")
        det = pending("extract", ["pending"])
        llm = pending("extract", ["pending_llm"])
        fig = pending("figures", ["pending"])
        pg = pages_left()
        print(f"[mine {it}] pages={pg} figures={fig} det={det} "
              f"llm={llm}", flush=True)
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
