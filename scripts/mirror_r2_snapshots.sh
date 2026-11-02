#!/usr/bin/env bash
# Mirror a list of R2 keys with one wrangler install and parallel gets.
#
#   bash scripts/mirror_r2_snapshots.sh KEYS_FILE DEST_ROOT
#
# KEYS_FILE is one object key per line (snapshots/{sport}/{season}/{week}/{run_id}.json).
# Each object is written to DEST_ROOT/key, the same path the serial `wrangler r2 object get
# --file` loop used. Bytes are whatever wrangler writes; this script does not transform them.
#
# wrangler is installed once (or $WRANGLER is used). Fetches run MIRROR_PARALLEL at a time
# (default 8). A non-zero wrangler status, including a missing object, fails that key.
# The step fails unless every listed key is on disk and the mirrored count is non-empty.
# Wrangler logs are printed only after a failed attempt, with the API token redacted.
# Requires R2_BUCKET. CLOUDFLARE_API_TOKEN stays in the environment for wrangler.

set -euo pipefail

scrub_file() {
  local file="$1"
  python3 - "$file" <<'PY'
import os, re, sys
path = sys.argv[1]
token = os.environ.get("CLOUDFLARE_API_TOKEN") or ""
try:
    text = open(path, encoding="utf-8", errors="replace").read()
except OSError:
    print("(log unreadable)", file=sys.stderr)
    sys.exit(1)
if token:
    text = text.replace(token, "***")
text = re.sub(r"(?i)authorization:\s*bearer\s+\S+", "Authorization: Bearer ***", text)
if len(text) > 4000:
    text = text[-4000:]
sys.stdout.write(text)
if text and not text.endswith("\n"):
    sys.stdout.write("\n")
PY
}

fetch_one() {
  local key="$1"
  [[ -n "${key//[[:space:]]/}" ]] || return 0
  local dest="${DEST_ROOT}/${key}"
  local attempts="${MIRROR_ATTEMPTS:-3}"
  local retry_sleep="${MIRROR_RETRY_SLEEP:-2}"
  local attempt log
  mkdir -p "$(dirname "$dest")" || return 1
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    log="$(mktemp)"
    if "$WRANGLER" r2 object get "${R2_BUCKET}/${key}" --file="${dest}" --remote >"$log" 2>&1; then
      rm -f "$log"
      if [[ -f "$dest" ]]; then
        return 0
      fi
      echo "::error::R2 snapshot fetch failed for ${key} (wrangler wrote no file)"
      return 1
    fi
    rm -f "$dest"
    if [[ "$attempt" -lt "$attempts" ]]; then
      echo "::warning::R2 snapshot fetch failed for ${key} (attempt ${attempt}/${attempts})"
      rm -f "$log"
      sleep "$retry_sleep"
    else
      echo "::error::R2 snapshot fetch failed for ${key}"
      scrub_file "$log" || echo "::error::R2 snapshot log suppressed for ${key}"
      rm -f "$log"
      return 1
    fi
  done
}

if [[ "${1:-}" == "--fetch-one" ]]; then
  # set -e would hide the status if a later `exit 0` ran after a failed fetch.
  set +e
  fetch_one "${2:-}"
  exit $?
fi

keys_file="${1:?snapshot key list required}"
DEST_ROOT="${2:-data}"
PARALLEL="${MIRROR_PARALLEL:-8}"
ATTEMPTS="${MIRROR_ATTEMPTS:-3}"
: "${R2_BUCKET:?R2_BUCKET is required}"

if ! [[ "$PARALLEL" =~ ^[0-9]+$ ]] || [[ "$PARALLEL" -lt 1 || "$PARALLEL" -gt 32 ]]; then
  echo "::error::MIRROR_PARALLEL must be an integer from 1 to 32"
  exit 1
fi
if ! [[ "$ATTEMPTS" =~ ^[0-9]+$ ]] || [[ "$ATTEMPTS" -lt 1 || "$ATTEMPTS" -gt 5 ]]; then
  echo "::error::MIRROR_ATTEMPTS must be an integer from 1 to 5"
  exit 1
fi

if [[ ! -f "$keys_file" || ! -s "$keys_file" ]]; then
  echo "::error::snapshot key list is empty"
  exit 1
fi

filtered="$(mktemp)"
trap 'rm -f "$filtered"' EXIT
grep -ve '^[[:space:]]*$' "$keys_file" >"$filtered" || true
listed="$(wc -l <"$filtered" | tr -d '[:space:]')"
if [[ "$listed" -lt 1 ]]; then
  echo "::error::snapshot key list is empty"
  exit 1
fi

if [[ -z "${WRANGLER:-}" ]]; then
  prefix="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/football-wrangler"
  mkdir -p "$prefix"
  npm install --no-save --prefix "$prefix" --no-fund --no-audit wrangler@4
  WRANGLER="$prefix/node_modules/.bin/wrangler"
fi
if [[ ! -x "$WRANGLER" ]]; then
  echo "::error::wrangler binary is not executable"
  exit 1
fi
version="$("$WRANGLER" --version 2>&1)" || {
  echo "::error::wrangler --version failed"
  exit 1
}
echo "using wrangler: ${version%%$'\n'*}"

export WRANGLER R2_BUCKET DEST_ROOT
export MIRROR_ATTEMPTS="$ATTEMPTS"

echo "fetching ${listed} snapshot(s) with ${PARALLEL} workers"
set +e
xargs -d $'\n' -P "$PARALLEL" -n 1 -r -a "$filtered" bash "$0" --fetch-one
rc=$?
set -e

mirrored=0
while IFS= read -r key || [[ -n "$key" ]]; do
  [[ -n "${key//[[:space:]]/}" ]] || continue
  if [[ -f "${DEST_ROOT}/${key}" ]]; then
    mirrored=$((mirrored + 1))
  else
    echo "::error::missing snapshot after mirror: ${key}"
  fi
done <"$filtered"

echo "mirrored ${mirrored} snapshot object(s) of ${listed} listed"
if [[ "$rc" -ne 0 || "$mirrored" -ne "$listed" || "$listed" -lt 1 ]]; then
  echo "::error::mirrored count ${mirrored} does not match listed ${listed} (fetch status ${rc})"
  exit 1
fi
