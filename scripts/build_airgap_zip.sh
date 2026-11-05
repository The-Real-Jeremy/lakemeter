#!/usr/bin/env bash
# Build the zip to carry into a workspace with no internet: the deployable app folder
# (backend/) with its prebuilt frontend, prices and vendored wheels, plus
# MANIFEST.sha256 to check the copy that arrives.
#
#   scripts/build_airgap_zip.sh [out.zip]      # default: dist/lakemeter-sqlite-app.zip
#
# Rebuilds the frontend first (needs npm and internet on this machine). The wheels are
# already in backend/wheels; refresh them with scripts/vendor_wheels.sh.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$repo/dist/lakemeter-sqlite-app.zip}"
(cd "$repo/frontend" && npm ci --no-audit --no-fund >/dev/null && npm run build >/dev/null)
stage="$(mktemp -d)"
app="$stage/lakemeter-sqlite-app"
mkdir -p "$app"
(cd "$repo/backend" && tar --exclude '__pycache__' --exclude '*.pyc' --exclude '.env' -cf - .) \
  | (cd "$app" && tar -xf -)
if find "$app" -type f -size +10M | grep -q .; then
  echo "files over the 10 MB Databricks Apps limit:" >&2
  find "$app" -type f -size +10M >&2
  exit 1
fi
(cd "$app" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z \
  | xargs -0 shasum -a 256 > MANIFEST.sha256)
mkdir -p "$(dirname "$out")"
rm -f "$out"
(cd "$stage" && zip -qr "$out" lakemeter-sqlite-app)
rm -rf "$stage"
echo "built $out ($(du -h "$out" | cut -f1))"
