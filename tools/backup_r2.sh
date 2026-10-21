#!/usr/bin/env bash
# backup_r2.sh — encrypted off-site backup (Artizen pattern).
#   working tree + .git  →  tar → zstd → gpg AES256 → chunks → R2 backups/<UTC>/
# Passphrase: ~/.config/esg-backup/passphrase (mode 600, OUTSIDE the repo).
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
KEYFILE="$HOME/.config/esg-backup/passphrase"
STAGE="$HOME/esg-backup-staging/$STAMP"
BUCKET="${ESG_BACKUP_BUCKET:-esg-verdicts-backups}"

mkdir -p "$STAGE" "$(dirname "$KEYFILE")"
chmod 700 "$(dirname "$KEYFILE")"
if [ ! -f "$KEYFILE" ]; then
  gpg --gen-rand 1 48 | base64 > "$KEYFILE"
  chmod 600 "$KEYFILE"
  echo "generated passphrase at $KEYFILE — store a copy in your password manager"
fi

tar -czf - \
  --exclude='./data/raw' --exclude='./.venv' --exclude='./node_modules' \
  --exclude='./esg-backup-staging' . \
  | zstd -T0 -3 \
  | gpg --batch --yes --symmetric --cipher-algo AES256 \
        --passphrase-file "$KEYFILE" \
  | split -b 4G - "$STAGE/esg-backup-$STAMP.tar.zst.gpg.part-"

echo "backup staged at $STAGE"
if command -v rclone >/dev/null && rclone listremotes 2>/dev/null | grep -q r2:; then
  rclone copy "$STAGE" "r2:$BUCKET/backups/$STAMP/"
  echo "uploaded to r2:$BUCKET/backups/$STAMP/"
else
  echo "rclone/r2 not configured — copy $STAGE off-box manually (encrypted, safe to store anywhere)"
fi
