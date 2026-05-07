#!/usr/bin/env bash
# SessionStart hook (matchers: compact, clear, resume).
# Restores the latest /handoff brief for the current cwd as one-shot context
# injection so the resumed session picks up where it left off.
#
# Safeguards (each addresses a real bug we hit):
#
#   1. ONE-SHOT CONSUME — after injecting, rename `latest-<slug>.md` to
#      `consumed-<session>-<ts>.md`. Without this, a second `/clear` in the
#      same cwd (or worse, an unrelated new session sharing the cwd) re-injects
#      the same brief, producing ghost context. To restore again, run /handoff.
#
#   2. FRESHNESS WINDOW — refuse to inject briefs older than
#      CLAUDE_COMPACTION_RESTORE_WINDOW seconds (default 1800 = 30min). Catches
#      the case where the symlink survived from yesterday's session.
#
#   3. VISIBLE HEADER — prepend `[Restored from /handoff brief — <ts>]` so the
#      user (and the model) can see the source of the injected context. No
#      more "why did /clear keep my prior session" surprises.
#
# Wire into ~/.claude/settings.json under "hooks.SessionStart". Recommended
# matchers: "compact" and "resume". The "clear" matcher is opt-in — set
# CLAUDE_COMPACTION_RESTORE_ON_CLEAR=1 if you want /clear to also restore.
set -euo pipefail

BRIEF_DIR="${CLAUDE_COMPACTION_BRIEF_DIR:-$HOME/.claude/compaction}"
MAX_BYTES="${CLAUDE_COMPACTION_MAX_BYTES:-25000}"
RESTORE_WINDOW="${CLAUDE_COMPACTION_RESTORE_WINDOW:-1800}"
RESTORE_ON_CLEAR="${CLAUDE_COMPACTION_RESTORE_ON_CLEAR:-0}"

input=$(cat)
session=$(echo "$input" | jq -r '.session_id // empty')
cwd=$(echo "$input" | jq -r '.cwd // empty')
source=$(echo "$input" | jq -r '.source // empty')

# Refuse the "clear" source by default — /clear semantically means wipe slate.
# User opts in via CLAUDE_COMPACTION_RESTORE_ON_CLEAR=1.
if [ "$source" = "clear" ] && [ "$RESTORE_ON_CLEAR" != "1" ]; then
  exit 0
fi

# Resolve a candidate brief — cwd-keyed symlink first, session-keyed fallback.
brief=""
if [ -n "$cwd" ]; then
  slug="${cwd//\//-}"
  by_cwd="$BRIEF_DIR/latest-${slug}.md"
  if [ -L "$by_cwd" ] || [ -f "$by_cwd" ]; then
    brief="$by_cwd"
  fi
fi
if [ -z "$brief" ] && [ -n "$session" ]; then
  candidate="$BRIEF_DIR/${session}.md"
  if [ -f "$candidate" ]; then
    brief="$candidate"
  fi
fi

if [ -z "$brief" ] || { [ ! -f "$brief" ] && [ ! -L "$brief" ]; }; then
  exit 0
fi

# Resolve symlink to its real target (mtime is on the target, not the link).
target="$(readlink -f "$brief" 2>/dev/null || echo "$brief")"

# Freshness check.
if [ -n "${RESTORE_WINDOW:-}" ] && [ "$RESTORE_WINDOW" -gt 0 ] 2>/dev/null; then
  now_s=$(date +%s)
  if stat -f %m "$target" >/dev/null 2>&1; then
    mtime_s=$(stat -f %m "$target")
  else
    mtime_s=$(stat -c %Y "$target")
  fi
  age=$((now_s - mtime_s))
  if [ "$age" -gt "$RESTORE_WINDOW" ]; then
    exit 0
  fi
  brief_iso=$(date -r "$mtime_s" -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -d "@$mtime_s" +"%Y-%m-%dT%H:%M:%SZ")
else
  age=0
  brief_iso="(unknown)"
fi

# Emit visible header so the user knows context was restored.
printf "[Restored from /handoff brief — written %s, %ds ago, source=%s]\n\n" \
  "$brief_iso" "$age" "${source:-unknown}"

head -c "$MAX_BYTES" "$target"

# One-shot consume — rename the cwd symlink so re-injection requires a fresh
# /handoff. Session-keyed fallback files are left in place (legacy path).
if [ -n "${by_cwd:-}" ] && { [ -L "$by_cwd" ] || [ -f "$by_cwd" ]; } && [ "$brief" = "$by_cwd" ]; then
  consumed="$BRIEF_DIR/consumed-${session:-unknown}-$(date +%Y%m%dT%H%M%S).md"
  mv "$by_cwd" "$consumed" 2>/dev/null || true
fi
