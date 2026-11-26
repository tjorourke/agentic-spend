#!/bin/bash
# Push a Grok Bot usage snapshot to a locally running agentic-spend server.
# Fallback: write the snapshot straight into ~/.agentic-spend/grokbot-usage.json
# if the server is not up. This is an optional automation path — on a Mac
# that has the Grok Bot app, the dashboard reads the app's local cache on
# its own and does not need this script.
#
# Usage:
#   echo '{"accountId":"work","plan":"Pro","weeklyPct":12.5,"resetAt":"2026-01-05T14:30:00+00:00","creditsUsd":1.25}' \
#     | grokbot-push.sh
#   cat report.txt | grokbot-push.sh          # raw usage-report text
#   grokbot-push.sh snapshot.json
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Installed as libexec/scripts/grokbot-push.sh, or run from a git checkout.
if [[ -f "$SCRIPT_DIR/../grokbot_usage.py" ]]; then
  ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [[ -f "$SCRIPT_DIR/grokbot_usage.py" ]]; then
  ROOT="$SCRIPT_DIR"
else
  echo "grokbot-push: cannot find grokbot_usage.py next to this script" >&2
  exit 1
fi

PORT="${AGENTIC_SPEND_PORT:-8787}"
URL="http://127.0.0.1:${PORT}/api/grokbot"

if [[ $# -gt 0 ]]; then
  BODY="$(cat -- "$1")"
else
  BODY="$(cat)"
fi

if [[ -z "${BODY// }" ]]; then
  echo "grokbot-push: stdin (or the file) is empty" >&2
  exit 1
fi

# Prefer the running server so the dashboard cache invalidates immediately.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
set +e
HTTP_CODE="$(printf '%s' "$BODY" | curl -sS -o "$tmp" -w '%{http_code}' \
  -X POST "$URL" \
  -H 'Content-Type: text/plain; charset=utf-8' \
  --data-binary @- \
  --connect-timeout 1 \
  --max-time 5 2>"$tmp.err")"
CURL_STATUS=$?
set -e

if [[ $CURL_STATUS -eq 0 && "$HTTP_CODE" == "200" ]]; then
  cat "$tmp"
  echo
  exit 0
fi

# Server down or rejected: write the file directly.
if ! printf '%s' "$BODY" | python3 "$ROOT/grokbot_usage.py"; then
  if [[ $CURL_STATUS -eq 0 ]]; then
    echo "grokbot-push: server returned HTTP $HTTP_CODE" >&2
    cat "$tmp" >&2 || true
  elif [[ -s "$tmp.err" ]]; then
    echo "grokbot-push: server not reachable, and writing the file failed" >&2
  fi
  exit 1
fi
