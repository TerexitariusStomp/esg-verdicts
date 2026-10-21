#!/usr/bin/env bash
# restore_r2.sh — restore an encrypted backup produced by backup_r2.sh.
# usage: restore_r2.sh <backup-dir-or-r2-path> [dest-dir]
set -euo pipefail
SRC="${1:?usage: restore_r2.sh <backup-dir> [dest-dir]}"
DEST="${2:-$HOME/esg-restore-$(date -u +%s)}"
KEYFILE="$HOME/.config/esg-backup/passphrase"

mkdir -p "$DEST"
if [[ "$SRC" == r2:* ]]; then
  LOCAL="$HOME/esg-restore-staging-$(date -u +%s)"
  rclone copy "$SRC" "$LOCAL"
  SRC="$LOCAL"
fi

cat "$SRC"/esg-backup-*.part-* | \
  gpg --batch --yes --decrypt --passphrase-file "$KEYFILE" | \
  zstd -d -T0 | tar -xzf - -C "$DEST"

echo "restored to $DEST"
