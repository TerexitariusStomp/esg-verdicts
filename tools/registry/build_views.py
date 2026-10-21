#!/usr/bin/env python3
"""build_views.py — regenerate <!-- GENERATED --> views in docs from registry.
Mirrors case-kit: markdown files embed generated tables between markers:
  <!-- GENERATED:figures --> ... <!-- /GENERATED -->
"""
import csv
import os
import re
import sys
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def figures_view():
    fp = os.path.join(ROOT, "registry", "figures.yaml")
    if not os.path.exists(fp):
        return ""
    figs = yaml.safe_load(open(fp))["figures"]
    lines = ["| Figure | Value |", "|---|---|"]
    for k, f in figs.items():
        lines.append(f"| {k} | {f.get('value',0)} |")
    return "\n".join(lines)


def flags_view():
    fp = os.path.join(ROOT, "registry", "flags.csv")
    if not os.path.exists(fp) or os.path.getsize(fp) == 0:
        return ""
    rows = list(csv.DictReader(open(fp)))
    lines = ["| Company | Flag | Severity | Year |", "|---|---|---|---|"]
    for r in rows[:100]:
        lines.append(f"| {r['company_id']} | {r['flag']} | "
                     f"{r['severity']} | {r['year']} |")
    return "\n".join(lines)


VIEWS = {"figures": figures_view, "flags": flags_view}


def regenerate(path):
    text = open(path).read()
    changed = False
    for name, fn in VIEWS.items():
        pat = (rf"(<!--\s*GENERATED:{name}\s*-->).*?"
               rf"(<!--\s*/GENERATED\s*-->)")
        new = fn()
        if re.search(pat, text, re.S):
            text2 = re.sub(pat, rf"\1\n{new}\n\2", text, flags=re.S)
            if text2 != text:
                changed = True
                text = text2
    if changed:
        open(path, "w").write(text)
    return changed


def main():
    n = 0
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "data", "evidence", "ops", ".venv", "node_modules")]
        for fn in files:
            if fn.endswith(".md"):
                if regenerate(os.path.join(root, fn)):
                    n += 1
                    print("updated", os.path.join(root, fn))
    print(f"views: {n} docs regenerated")


if __name__ == "__main__":
    main()
