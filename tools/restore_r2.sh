#!/usr/bin/env bash
# restore_r2.sh — restore an encrypted backup produced by backup_r2.sh.
# usage: restore_r2.sh <backup-dir-or-r2-path> [dest-dir]
set -euo pipefail
SRC="${1:?usage: restore_r2.sh <backup-dir> [dest-dir]}"
DEST="${2:-$HOME/esg-restore-$(date -u +%s)}"
KEYFILE="$HOME/.config/esg-backup/passphrase"

mkdir -p "$DEST"
if [[ "$SRC" == r2://* ]]; then
  LOCAL="$HOME/esg-restore-staging-$(date -u +%s)"
  mkdir -p "$LOCAL"
  WRANGLER="${WRANGLER:-$HOME/.local/bin/wrangler}"
  ACCT="${CLOUDFLARE_ACCOUNT_ID:-6c0140469651854b2e358517371a03d3}"
  BUCKET_PATH="${SRC#r2://}"
  STAMP_PART="$(basename "$BUCKET_PATH")"
  SUFFIXES=$(python3 -c "
import itertools,string
for c in itertools.product(string.ascii_lowercase,repeat=2):
    print(''.join(c))")
  GOT=0
  for SUFFIX in $SUFFIXES; do
    OBJ="$BUCKET_PATH/esg-backup-$STAMP_PART.tar.zst.gpg.part-$SUFFIX"
    CLOUDFLARE_ACCOUNT_ID="$ACCT" "$WRANGLER" r2 object get "$OBJ" \
      --file "$LOCAL/part-$SUFFIX" >/dev/null 2>&1 || break
    GOT=1
  done
  [ "$GOT" = 1 ] || { echo "no backup found at $SRC"; exit 1; }
  SRC="$LOCAL"
fi

cat "$SRC"/*part-* | \
  gpg --batch --yes --decrypt --passphrase-file "$KEYFILE" | \
  zstd -d -T0 | tar -xzf - -C "$DEST"

echo "restored to $DEST"
