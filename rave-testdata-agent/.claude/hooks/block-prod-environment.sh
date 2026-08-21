#!/usr/bin/env bash
# PreToolUse guardrail: refuse a command that targets a production Rave study.
#
# The Python config loader already refuses production (C-1, SEC-4). This is the
# second, deterministic line: it fires before the tool runs, so a hand-written
# command that bypasses the loader is still stopped. Neither layer relies on the
# model's judgement.
#
# Exit 2 blocks the tool call; anything else lets it through.
set -uo pipefail

payload="$(cat)"

# The hook receives a JSON envelope; extract the command rather than matching
# the raw JSON.
command="$(printf '%s' "$payload" | python -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
found = data.get("command") or (data.get("tool_input") or {}).get("command") or ""
print(found)
' 2>/dev/null)"
[ -z "${command:-}" ] && command="$payload"
command="$(printf '%s' "$command" | tr '[:upper:]' '[:lower:]')"

# Only inspect commands that could reach Rave.
case "$command" in
  *rave*|*rws*|*mdsol*|*run_submit*|*run_provision*|*run_dynamics*|*run_all*) ;;
  *) exit 0 ;;
esac

# A study addressed as NAME(PROD), or an environment set to a production value.
if printf '%s' "$command" | grep -Eq \
   '\((prod|production|prd|live)\)|environment[=:][[:space:]]*"?(prod|production|prd|live)([[:space:]"},]|$)|--environment[=[:space:]]+(prod|production|prd|live)([[:space:]]|$)'; then
  echo "BLOCKED: this command targets a production Rave environment." >&2
  echo "Writing synthetic data to production is a GxP violation risk (C-1, SEC-4)." >&2
  echo "No configuration flag unblocks it - use a Dev, Sandbox, UAT or Training study." >&2
  exit 2
fi

exit 0
