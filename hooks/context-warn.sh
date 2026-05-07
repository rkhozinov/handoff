#!/usr/bin/env bash
# Stop hook: warn the user when the session's input-token usage crosses a
# configurable threshold of the model context window. Claude Code has no
# native "context X% full" event, so this approximates by parsing the last
# assistant entry's `usage` field from the live transcript.
#
# When fired, this prints a one-line warning to stdout, which CC surfaces
# back to the user as a stop-hook notification — they can then choose to
# run /handoff before context overflows.
#
# Wire into ~/.claude/settings.json under "hooks.Stop":
#   {"matcher": "", "hooks": [{"type":"command","command":"<this-file>"}]}
#
# Env:
#   CLAUDE_COMPACTION_CONTEXT_THRESHOLD — fraction (0.0-1.0), default 0.70
#   CLAUDE_COMPACTION_CONTEXT_WINDOW    — override window in tokens
set -euo pipefail

THRESHOLD="${CLAUDE_COMPACTION_CONTEXT_THRESHOLD:-0.70}"
OVERRIDE_WINDOW="${CLAUDE_COMPACTION_CONTEXT_WINDOW:-}"

input=$(cat)
transcript=$(echo "$input" | jq -r '.transcript_path // empty')

if [ -z "$transcript" ] || [ ! -f "$transcript" ]; then
  exit 0
fi

# Parse the last assistant entry's usage. cache_read + cache_creation +
# input_tokens approximates the model's effective input context size.
read -r tokens model <<<"$(
  jq -r 'select(.type == "assistant" and (.message.usage // null) != null)
         | "\((.message.usage.cache_read_input_tokens // 0)
              + (.message.usage.cache_creation_input_tokens // 0)
              + (.message.usage.input_tokens // 0)) \(.message.model // "")"' \
    "$transcript" | tail -n 1
)"

if [ -z "${tokens:-}" ] || [ "$tokens" = "0" ]; then
  exit 0
fi

# Pick context window by model. Opus[1M] = 1_000_000, others default 200_000.
if [ -n "$OVERRIDE_WINDOW" ]; then
  window="$OVERRIDE_WINDOW"
elif [[ "${model:-}" == *"1m"* ]] || [[ "${model:-}" == *"[1M]"* ]]; then
  window=1000000
else
  window=200000
fi

ratio=$(awk -v t="$tokens" -v w="$window" 'BEGIN { printf "%.4f", t / w }')
threshold_pct=$(awk -v r="$THRESHOLD" 'BEGIN { printf "%.0f", r * 100 }')
ratio_pct=$(awk -v r="$ratio" 'BEGIN { printf "%.0f", r * 100 }')

over=$(awk -v r="$ratio" -v t="$THRESHOLD" 'BEGIN { print (r >= t) ? 1 : 0 }')
if [ "$over" = "1" ]; then
  printf "⚠️  Context at %s%% (%s/%s tokens, threshold %s%%). Run /handoff then /clear before context overflows.\n" \
    "$ratio_pct" "$tokens" "$window" "$threshold_pct"
fi
