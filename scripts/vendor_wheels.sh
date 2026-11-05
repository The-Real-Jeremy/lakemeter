#!/usr/bin/env bash
# Re-download backend/wheels for the offline install. Run on a machine with internet access
# whenever backend/requirements.txt changes, then commit backend/wheels.
#
#   scripts/vendor_wheels.sh          # PY=python3.12 chooses the interpreter that runs pip
#
# backend/wheels holds only what the Databricks Apps Python 3.11 runtime does NOT pre-install:
# SQLAlchemy, pydantic-settings, XlsxWriter and the Databricks SDK, plus the small pure-Python
# dependencies they need recent versions of. FastAPI, Uvicorn, pydantic, httpx, cryptography and
# the other pre-installed libraries come from the runtime ("Requirement already satisfied"), which
# keeps the repo ZIP under the 10 MB workspace-import limit. If a runtime ever lacks one, the
# deploy stops and names the package: add it to VENDOR below and run this again.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-python3}"
VENDOR=(
  "sqlalchemy>=2.0.0" "pydantic-settings>=2.1.0" "xlsxwriter>=3.1.9" "databricks-sdk>=0.123.0"
  certifi python-dotenv typing-inspection typing-extensions urllib3 requests idna
  google-auth pyasn1 pyasn1-modules
)
new="$(mktemp -d)"
# --platform any: pure-Python wheels only (SQLAlchemy ships one; no compiled code to match).
"$PY" -m pip download --disable-pip-version-check --only-binary=:all: --no-deps \
  --platform any --python-version 3.11 --implementation py --abi none \
  -d "$new" "${VENDOR[@]}" >/dev/null
rm -rf "$repo/backend/wheels"
mv "$new" "$repo/backend/wheels"
echo "backend/wheels: $(ls "$repo/backend/wheels" | wc -l | tr -d ' ') wheels, $(du -sh "$repo/backend/wheels" | cut -f1)"
