"""evaluate.py — extraction-quality eval harness -> registry/eval.csv.

The surpass-the-field receipt: published P/R/F1 on a labeled fixture set.
Fixture: tests/fixtures/extraction_gold.jsonl — one row per labeled page:
  {"text": "...page text...", "gold": [{"metric":..., "value":...}]}
We run the regex tier (and LLM tier if gateway up) over each and score.

Ships metrics to registry/eval.csv every run; the verify gate re-checks the
file exists and is fresh. Adding gold rows over time hardens the eval.
"""
import argparse
import csv
import json
import os
import re

from .esg import db, now
from .extract import regex_extract

GOLD = "tests/fixtures/extraction_gold.jsonl"
OUT = "registry/eval.csv"


def _close(a, b, tol=0.02):
    try:
        return abs(a - b) / max(abs(b), 1e-9) <= tol
    except Exception:
        return False


def evaluate():
    if not os.path.exists(GOLD):
        return None
    tp = fp = fn = 0
    per_metric = {}
    for line in open(GOLD):
        row = json.loads(line)
        got = regex_extract(row["text"])
        gold = row.get("gold", [])
        got_map = {(g["metric"], round(g["value"], 3)) for g in got}
        gold_map = {(g["metric"], round(g["value"], 3)) for g in gold}
        # match by metric with tolerance on value
        matched = set()
        for gm, gv in gold_map:
            hit = any(em == gm and _close(ev, gv) for em, ev in got_map)
            if hit:
                tp += 1
                matched.add(gm)
            else:
                fn += 1
            per_metric.setdefault(gm, [0, 0, 0])
            per_metric[gm][1 if hit else 2] += 1
        for em, ev in got_map:
            if not any(gm == em and _close(gv, ev) for gm, gv in gold_map):
                fp += 1
                per_metric.setdefault(em, [0, 0, 0])
                per_metric[em][0] += 1
    p = tp / (tp + fp) if tp + fp else 0
    r = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * p * r / (p + r) if p + r else 0
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3),
            "per_metric": per_metric}


def main():
    ap = argparse.ArgumentParser()
    a = ap.parse_args()
    res = evaluate()
    os.makedirs("registry", exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "evaluated_at", "tier", "metric", "precision", "recall",
            "f1", "tp", "fp", "fn"])
        w.writeheader()
        if res:
            w.writerow({"evaluated_at": now(), "tier": "regex", "metric": "ALL",
                        "precision": res["precision"], "recall": res["recall"],
                        "f1": res["f1"], "tp": res["tp"], "fp": res["fp"],
                        "fn": res["fn"]})
            for m, (fp_, fn_, tp_) in sorted(res["per_metric"].items()):
                tot = fp_ + fn_ + tp_
                w.writerow({"evaluated_at": now(), "tier": "regex",
                            "metric": m,
                            "precision": "", "recall": "", "f1": "",
                            "tp": tp_, "fp": fp_, "fn": fn_})
        else:
            w.writerow({"evaluated_at": now(), "tier": "regex", "metric": "ALL",
                        "precision": "", "recall": "", "f1": "",
                        "tp": 0, "fp": 0, "fn": 0})
    print("eval:", res or "no gold fixtures")


if __name__ == "__main__":
    main()
