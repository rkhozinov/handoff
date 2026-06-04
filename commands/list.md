---
description: List session briefs grouped by status. Default hides done. Pass --all to include done. Filters by current cwd unless --any-cwd.
argument-hint: "[--all] [--any-cwd]"
---

Scan `~/.claude/compaction/*.md`, parse each brief's frontmatter, group by
status. One row per brief: short sid, age, status badge, cwd basename, and the
first line of the trimmed convo (first `U: …` line) as a goal hint.

## Render

```bash
ARG="$ARGUMENTS"
SHOW_DONE=0
ANY_CWD=0
case " $ARG " in
  *' --all '*|*' --all') SHOW_DONE=1 ;;
esac
case " $ARG " in
  *' --any-cwd '*|*' --any-cwd') ANY_CWD=1 ;;
esac

COMPACTION_DIR="$HOME/.claude/compaction"
CUR_CWD="$(pwd -P)"

# Newest first.
FILES=$(ls -t "$COMPACTION_DIR"/*.md 2>/dev/null \
        | grep -E '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.md$' \
        | grep -v -E '/consumed-|-full\.md$')

emit_row() {
  local sid="$1" status="$2" age="$3" cwd="$4" goal="$5"
  local short="${sid:0:8}"
  local badge
  case "$status" in
    done)        badge="✓ done       " ;;
    pending)     badge="? pending    " ;;
    in_progress) badge="… in_progress" ;;
    *)           badge="$status        " ;;
  esac
  printf '  %s  %s  %s  %s\n' "$short" "$badge" "$age" "${cwd##*/}"
  [ -n "$goal" ] && printf '              %s\n' "$(printf '%s' "$goal" | cut -c1-120)"
}

printf '\nSession briefs in %s\n' "$COMPACTION_DIR"
[ "$SHOW_DONE" -eq 0 ] && printf '(hiding done — pass --all to include)\n'
[ "$ANY_CWD" -eq 0 ] && printf '(cwd=%s — pass --any-cwd to widen)\n' "$CUR_CWD"
printf '\n'

declare -i n_pending=0 n_inprog=0 n_done=0

for f in $FILES; do
  head -20 "$f" | grep -q '^---$' || continue

  status=$(awk '/^---$/{c++;next} c==1 && /^status:/{print $2; exit}' "$f")
  cwd=$(awk '/^---$/{c++;next} c==1 && /^cwd:/{sub(/^cwd: */,""); print; exit}' "$f")
  # Prefer frontmatter created over file mtime — mtime gets bumped on every
  # /hand:off re-run, frontmatter created is preserved across them.
  age=$(awk '/^---$/{c++;next} c==1 && /^created:/{sub(/^created: */,""); print; exit}' "$f")
  [ -z "$age" ] && age=$(stat -f '%Sm' -t '%Y-%m-%dT%H:%MZ' "$f" 2>/dev/null \
                         || stat -c '%y' "$f" 2>/dev/null | cut -c1-16)
  sid=$(basename "$f" .md)
  # Prefer the recap frontmatter (LLM or extracted) over the raw first user msg.
  goal=$(awk '/^---$/{c++;next} c==1 && /^recap:/{sub(/^recap: */,""); print; exit}' "$f")
  if [ -z "$goal" ] || [ "$goal" = "null" ]; then
    goal=$(awk '/^U:/{sub(/^U: */,""); print; exit}' "$f")
  fi

  if [ "$ANY_CWD" -eq 0 ] && [ -n "$cwd" ] && [ "$cwd" != "$CUR_CWD" ]; then
    continue
  fi

  case "$status" in
    done)
      n_done=$((n_done+1))
      [ "$SHOW_DONE" -eq 1 ] && emit_row "$sid" "$status" "$age" "$cwd" "$goal"
      ;;
    pending)
      n_pending=$((n_pending+1))
      emit_row "$sid" "$status" "$age" "$cwd" "$goal"
      ;;
    in_progress)
      n_inprog=$((n_inprog+1))
      emit_row "$sid" "$status" "$age" "$cwd" "$goal"
      ;;
    *)
      # No status set yet (legacy brief w/o frontmatter)
      emit_row "$sid" "?" "$age" "$cwd" "$goal"
      ;;
  esac
done

printf '\n  pending: %d  in_progress: %d  done: %d\n' \
  "$n_pending" "$n_inprog" "$n_done"
```

Show the user the rendered table verbatim — no extra commentary needed. If
they want to resume one, they'll run `/hand:on <sid>` themselves.
