# handoff

Deterministic transcript trimmer + memory archive that replaces Claude Code's
lossy default `/compact` for long sessions. Slash commands: `/hand:off` and `/hand:on`.

## Why

Claude Code's built-in `/compact` runs an LLM summarizer over the entire
conversation. That summarizer paraphrases code, forgets file paths, drops
direction reversals, and ignores user-supplied "preserve X, Y" hints
(those hints are appended as context, not used as summarizer instructions —
the summarizer prompt is fixed and not user-configurable in the CLI).

Real signal in a Claude Code session is:

| Keep verbatim | Drop |
|---|---|
| User messages (intent, decisions, reversals) | Tool result bodies |
| Substantive assistant text | Thinking blocks |
| Code blocks, file paths, line numbers | Procedural narration ("let me check") |
| Errors, exact strings | Repeated tool retries / dead ends |

This project does deterministic trimming instead of LLM paraphrasing, then
archives the **full** untrimmed transcript as a `memory doc` so anything
dropped is recoverable later via `memory doc search`.

## Architecture

```
/hand:off (explicit)
        │
        ▼
  trim_and_archive  ──┬──▶ memory doc store      (full session, recoverable)
                      └──▶ ~/.claude/compaction/<session>.md  (trimmed brief)
                                                  ▲
                                                  │
                                          /hand:on (explicit)
                                          Read tool → injected into context
```

User-facing flow:

```
[long session, ~70% context]
   ↓
/hand:off
   ↓
prints brief path + memory doc hash
   ↓
user runs /clear
   ↓
user runs /hand:on → loads brief into the new session
```

## Install

```bash
git clone https://github.com/<you>/handoff ~/repos/handoff
cd ~/repos/handoff
./install.sh
```

`install.sh` is idempotent: re-running upgrades the symlinks and patches
`~/.claude/settings.json` without duplicating hook entries.

## Usage

Run `/hand:off` when the context starts filling. It:

1. Archives the full session as a `memory doc` (recoverable via `memory doc search`)
2. Writes a deterministic trimmed brief to `~/.claude/compaction/<session_id>.md`
3. Auto-stores any sub-agent reports as memory entries

Briefs are keyed by `session_id`. Claude Code keeps `session_id` stable across
`/clear` and `claude -c` resume, so `/hand:on` deterministically restores the
right brief no matter which worktree or branch you're in. No symlinks, no
cwd-slug heuristics, no cross-branch ghosting.

Then run `/clear` for a fresh slate, and `/hand:on` when you want the brief
back. Restoration is always explicit — there is no SessionStart hook that
auto-injects context.

`/hand:on` reads the latest brief for the current cwd via the Read tool. It's
idempotent (re-runnable) and never consumes the symlink — use it whenever
you want to re-anchor.

If the trimmer dropped something you actually needed, recall the full archive:
`memory doc get <hash>`.

## Session lifecycle, recap, and the global log

Every brief starts with YAML frontmatter:

```yaml
---
status: in_progress       # pending | in_progress | done (auto-detected)
title: session-recap-automation   # CC's own ai-title from the transcript
session_id: <sid>
cwd: <abs path>
created: <iso8601>
last_resumed: <iso8601 or null>
completion_signal: auto-todowrite | auto-user-msg | auto-open-q | auto-default | manual | auto-stale
archive_hash: <memory doc hash>
recap: Goal: cut portal latency. LCP 3054→2202ms shipped. Next: merge PR #2511.
recap_source: llm         # llm (composed by /hand:off) | extracted (fallback)
---
```

- **status** is detected from the transcript (TodoWrite state, completion
  language in the last user messages, open questions). Conservative bias:
  uncertain → `in_progress`, never `done`. `/hand:done <sid>` flips it
  manually (sticky); `scripts/sweep_stale.py` auto-closes briefs idle
  longer than 14 days.
- **recap** is the one LLM-composed field: `/hand:off` writes a 1–2
  sentence `Goal → current → next` line and passes it via `--recap`.
  Direct CLI runs fall back to deterministic extraction (first signal
  user message + first open todo).
- **title** is CC's own generated session title, read from the transcript.

Every `/hand:off` also upserts one row per session into a SQLite index at
`~/.claude/compaction/sessions.db` — the queryable store of "what was I
doing across all projects, and how do I get back into it." It holds all
frontmatter fields plus the trimmed brief body, so the CLI and TUI review
a session without opening the `.md`. The brief files stay authoritative;
the DB mirrors them and is rebuildable from them:

```bash
PYTHONPATH=. python3 -m handoff.dbcli rebuild   # backfill / self-heal from brief files
```

`/hand:list` shows briefs grouped by status (current cwd by default,
`--any-cwd` to widen, `--all` to include done), using the recap as the
per-row goal hint. Under the hood it queries the DB via
`handoff.dbcli`, the same backend `/hand:on` and `/hand:done` use to keep
the brief file frontmatter and the DB row in sync.

### Session TUI

`/hand:tui` (or `hand tui`) launches a 2-pane Textual UI: left pane lists
handoffs (title, date, status, newest-first), right pane shows the
selected brief, scrollable. Keys: `j/k` navigate, `a` show/hide done, `d`
done, `r` reopen, `x` delete (DB row only), `g` refresh, `q` quit. Textual
is an optional dependency — install it with `pip install -e '.[tui]'`. A
richer CLI (`hand show`, `hand search`) is also available.

## Stats (real fixtures)

```
fixture        bytes_in  bytes_out  ratio   user_msgs_kept  decisions  files  code  errors
small.jsonl    560 KB    57 KB      10.18%   9/9              4         10    10    5
medium.jsonl    11 MB    96 KB       0.87%  58/58             6         42     2    5
large.jsonl     12 MB   634 KB       5.22% 144/144           23         91    45    5
huge.jsonl      14 MB   618 KB       4.24% 392/392           64        233    41    5
xhuge.jsonl     83 MB   2.6 MB       3.03% 911/911          154       1146   206    5
```

The brief is the trimmed conversation: verbatim user messages,
substantive assistant text, code fences, file paths, sub-agent reports,
Read-tool markers — drops tool_result bodies, thinking blocks, and
narration noise. Single output file: `~/.claude/compaction/<sid>.md`.

Reproduce: `PYTHONPATH=. python3 scripts/bench.py`

## Tests

```bash
pip install -e ".[dev]"
pytest                               # extract + trim + stats over real fixtures
PYTHONPATH=. python3 scripts/bench.py
PYTHONPATH=. python3 scripts/render_html.py   # docs/report.html visualization
```

`tests/fixtures/raw/` (gitignored) holds real transcripts collected via
`scripts/collect_fixtures.py`. `tests/fixtures/scrubbed/` holds redacted
versions safe to commit, produced by `scripts/scrub.py`.

## Hard invariant

Every real user message must appear verbatim in the brief. Tested across
every fixture. If you ever see this fail in `scripts/bench.py`, that's a
bug — the trimmer dropped user intent.

## What this can't do

- Replace the built-in summarizer prompt — only the API SDK exposes
  `instructions` on the compaction beta header (`compact-2026-01-12`),
  not the CC CLI.
- Auto-restore on `/clear` or `/resume` — `/hand:on` is always explicit by
  design (avoids ghost-context surprises across unrelated sessions in the
  same cwd).

## Context-fill warning

A `Stop` hook (`hooks/context-warn.sh`) parses the last assistant entry's
`usage` field after every turn and prints a one-line warning to the
session when input tokens cross the threshold:

    ⚠️  Context at 78% (157k/200k tokens, threshold 70%). Run /hand:off …

CC has no native "context X% full" event, so this approximates by reading
the live JSONL transcript. Lightweight (single `jq` pass on the last
assistant entry's usage block), fires once per turn.

## Configuration

Hook env vars:

- `HANDOFF_ROOT` — path to this repo (default `~/repos/handoff`)
- `HANDOFF_PYTHON` — python interpreter (default `python3`)
- `HANDOFF_BRIEF_DIR` — where briefs live (default `~/.claude/compaction`)
- `HANDOFF_MAX_BYTES` — `/hand:on` Read cap (default 25000)
- `HANDOFF_CONTEXT_THRESHOLD` — context-warn fraction (default `0.70`)
- `HANDOFF_CONTEXT_WINDOW` — model context window override in tokens
  (default 200000; set to 1000000 if running Opus 4.x with the 1M extended window)
