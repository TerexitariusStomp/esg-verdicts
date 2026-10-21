#!/usr/bin/env bash
# backup_r2.sh — encrypted off-site backup (Artizen pattern).
#   working tree + .git  →  tar → zstd → gpg AES256 → chunks → R2 backups/<UTC>/
# Passphrase: ~/.config/esg-backup/passphrase (mode 600, OUTSIDE the repo).
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
KEYFILE="$HOME/.config/esg-backup/passphrase"
STAGE="$HOME/esg-backup-staging/$STAMP"
BUCKET="${ESG_BACKUP_BUCKET:-esg-verdicts-backup}"
ACCOUNT_ID="${CLOUDFLARE_ACCOUNT_ID:-6c0140469651854b2e358517371a03d3}"

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
WRANGLER="${WRANGLER:-$HOME/.local/bin/wrangler}"
if [ -x "$WRANGLER" ]; then
  for f in "$STAGE"/esg-backup-*.part-*; do
    CLOUDFLARE_ACCOUNT_ID="$ACCOUNT_ID" "$WRANGLER" r2 object put \
      "$BUCKET/backups/$STAMP/$(basename "$f")" --file "$f"
  done
  echo "uploaded to r2://$BUCKET/backups/$STAMP/"
else
  echo "wrangler not found — copy $STAGE off-box manually (encrypted, safe to store anywhere)"
fi
