#!/usr/bin/env bash
# install.sh — write esg-*.service/.timer user units to ~/.config/systemd/user/
# and enable the timers. Cadence-tiered; sequential heavy stages via ordering.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
UNITDIR="$HOME/.config/systemd/user"
mkdir -p "$UNITDIR"

mk() {  # mk <name> <OnCalendar> <exec...>
  local name="$1" cal="$2"; shift 2
  cat > "$UNITDIR/esg-$name.service" <<EOF
[Unit]
Description=esg-verdicts $name
[Service]
Type=oneshot
WorkingDirectory=$REPO
Nice=15
MemoryMax=20G
ExecStart=$@
EOF
  cat > "$UNITDIR/esg-$name.timer" <<EOF
[Unit]
Description=esg-verdicts $name timer
[Timer]
OnCalendar=$cal
Persistent=true
[Install]
WantedBy=timers.target
EOF
  systemctl --user enable "esg-$name.timer" >/dev/null 2>&1 || true
  systemctl --user start "esg-$name.timer" >/dev/null 2>&1 || true
  echo "installed esg-$name.timer ($cal)"
}

# daily chain (sequential — later stages run after earlier finish, staggered
# hours so heavy stages never overlap)
mk catalog   "*-Mon 01:00" "$PY -m esg.catalog crawl --limit 0"
mk harvest   "*-*-* 02:00" "$PY -m esg.harvest --workers 6"
mk pages     "*-*-* 03:00" "$PY -m esg.pages"
mk figures   "*-*-* 03:45" "$PY -m esg.figures"
mk extract   "*-*-* 04:30" "$PY -m esg.extract"
mk filings   "*-*-* 05:00" "$PY -m esg.filings --limit 200"
mk verify    "*-*-* 06:00" "$PY -m esg.verify"
mk flags     "*-*-* 06:20" "$PY -m esg.flags"
mk export    "*-*-* 07:00" "$PY -m esg.export"
mk gate      "*-*-* 07:30" "$PY tools/verify/run_all.py"
mk stamp     "*-*-* 07:50" "bash -c 'cd $REPO && bash tools/refresh_manifest.sh && ots stamp MANIFEST.sha256 && ots stamp MANIFEST-meta.txt'"
mk backup    "*-*-* 08:00" "bash tools/backup_r2.sh"

# weekly
mk universe  "*-Mon 00:30" "$PY -m esg.universe --gleif --limit 500"
mk claims    "*-Tue 02:00" "$PY -m esg.claims --limit 2000"
mk greenwash "*-Wed 02:00" "$PY -m esg.greenwash --limit 1000"

# monthly / quarterly
mk geo       "*-*-01 01:00" "$PY -m esg.geo --limit 0"
mk litigate  "*-*-05 01:00" "$PY -m esg.litigate"
mk eval      "*-Sun 09:00" "$PY -m esg.evaluate"
mk restore   "*-*-15 04:00" "bash -c 'echo restore-check: manually verify quarterly per AGENTS.md'"

echo "done — systemctl --user list-timers esg-*"
