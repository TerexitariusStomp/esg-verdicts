#!/usr/bin/env bash
# refresh_manifest.sh — rebuild MANIFEST.sha256 (whole tree incl .git internals)
# and MANIFEST-meta.txt (manifest hash ⊕ git HEAD ⊕ export version).
# Rotates previous meta/ots to .prev for the custody chain.
set -euo pipefail
cd "$(dirname "$0")/.."

for f in MANIFEST-meta.txt MANIFEST.sha256.ots MANIFEST-meta.txt.ots; do
  [ -f "$f" ] && cp "$f" "$f.prev" || true
done

# Hash every file that exists on disk (tracked or not), including .git internals —
# repo history is part of the custody record. Exclude the manifest files themselves
# and volatile paths.
find . -type f \
  -not -path './MANIFEST.sha256' \
  -not -name '*.ots' \
  -not -path './data/*' \
  -not -path './evidence/*' \
  -not -path './ops/*' \
  -not -path './.venv/*' \
  -not -path './node_modules/*' \
  -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256

META_HASH=$(sha256sum MANIFEST.sha256 | cut -d' ' -f1)
GIT_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "no-git")
cat > MANIFEST-meta.txt <<EOF
manifest_sha256=$META_HASH
git_head=$GIT_HEAD
export_version=$(cat data/.export_version 2>/dev/null || echo "0")
stamped_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
echo "manifest: $META_HASH"
echo "meta: git_head=$GIT_HEAD"
