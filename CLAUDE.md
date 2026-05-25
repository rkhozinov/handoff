# handoff — Claude Code instructions

Read README.md first for project intent. This file = working notes for
Claude sessions in this repo.

## Architecture (single-brief, post-2026-05)

- One output file per /hand:off: `~/.claude/compaction/<session_id>.md`.
- Header (session, cwd, archive hash) + anti-re-read notice + trimmed
  conversation. **No tier1/tier2 split** — that was ripped on `a377986`
  after empirical review showed the trimmed convo more useful than the
  extracted-sections summary. Don't reintroduce tiers without strong
  evidence.
- The trimmer keeps verbatim: every signal user msg, every substantive
  assistant text turn (capped at `ASSISTANT_TURN_MAX_CHARS = 4_000`),
  every code fence, every Read marker, every sub-agent report.
- Drops: tool_result bodies, thinking blocks, narration ("let me check"),
  noise user msgs (acks, skill bodies, prior compaction continuations,
  `<task-notification>` async pings).

## Hard invariant

`signal_kept == user_signal` (100%) across all fixtures. Enforced by
`scripts/bench.py`. If a trimmer change drops a real user msg, that's a
regression — fix the filter, don't relax the invariant.

## Module layout

- `handoff/extract.py` — pure extraction helpers over JSONL entries
  (`is_real_user`, `user_text`, `assistant_blocks`, `iter_*_user_msgs`,
  `short_tool_input`, `extract_agent_reports`, `extract_decisions/...`
  used by bench/report stats only).
- `handoff/trim.py` — `render_brief(entries, sid, cwd, archive_hash)`.
  `build_convo` exposed for the report's audit panel.
- `handoff/cli.py` — orchestrator: load_jsonl → archive → render → write.
- `handoff/recall.py` — `project_tag_from_cwd` + `store_agent_reports`.
  That's it. Older `build_query`/`search_memories`/`format_memory_line`
  were ripped (no callers post-tier1).
- `handoff/tokenizer.py` — pluggable `count_tokens(text, mode)`:
  `chars4` (default fallback), `hf` (Xenova/claude-tokenizer), `api`
  (Anthropic SDK), `auto` (HF if importable else chars4). Lazy imports —
  don't hoist `transformers`/`anthropic` to module level.
- `handoff/__init__.py` — empty marker.
- `scripts/bench.py`, `scripts/render_html.py` — dev tools, not shipped
  via `/hand:off`.

## Don't reintroduce

- `handoff/segment.py` was deleted (B2 idea, never wired).
- `tier1`/`tier2` vocabulary in docstrings, comments, or symbols.
- `**_legacy_kwargs` shims on `render_brief`.
- Magic literals for the agent-report cutoff — use `AGENT_REPORT_MIN_CHARS`.

## Magic constants worth knowing

- `extract.AGENT_REPORT_MIN_CHARS = 200` — sub-agent reports below this
  are dropped (stub noise).
- `extract.PASTED_PRESERVE_CHARS = 200` — terminal-output paste elision.
- `extract.DEFAULT_TOOL_VALUE_LIMIT = 100`, `TOOL_VALUE_LIMITS["Bash"] = 60`.
- `trim.ASSISTANT_TURN_MAX_CHARS = 4_000`.
- `_ANTHROPIC_MODEL = "claude-opus-4-7"` in tokenizer.py — bump when
  newer model lands.

## Test + bench workflow

```bash
PYTHONPATH=. python3 -m pytest tests/ -q          # ~150 tests
PYTHONPATH=. python3 scripts/bench.py             # invariant check across 6 fixtures
PYTHONPATH=. python3 scripts/render_html.py       # docs/report.html (gitignored)
```

End-to-end smoke:

```bash
PYTHONPATH=. python3 -m handoff.cli \
  --transcript tests/fixtures/raw/small.jsonl \
  --session-id smoke --cwd /tmp \
  --no-archive --out-dir /tmp/smoke
```

`tests/fixtures/raw/` is gitignored (PII). Fixture-dependent tests skip
when raw fixtures are absent.

## /hand:off and /hand:on contracts

- `/hand:off` lives at `commands/off.md`. Uses `${CLAUDE_SESSION_ID}`
  to derive both the session id and the transcript path from one source
  — don't decouple, that bug surfaced on 2026-05-08 (brief content
  belonged to a different session than the filename).
- `/hand:on` lives at `commands/on.md`. Accepts `<session-id>` or a
  full path. Bare `/hand:on` walks newest jsonls in the cwd's project dir
  and Reads the first matching brief — non-deterministic when the cwd
  has many parallel sessions, so prefer passing the session id.
- `/hand:done <sid> [--reopen]` lives at `commands/done.md`. Manual
  status flip — sets `completion_signal: manual`, which is sticky: a
  later `/hand:off` on the same sid will NOT auto-revive it.
- `/hand:list [--all] [--any-cwd]` lives at `commands/list.md`. Reads
  frontmatter, groups by status, defaults to current cwd + hides done.

## Session lifecycle (frontmatter)

Every brief carries a YAML frontmatter block written by `render_brief`:

```yaml
---
status: in_progress       # pending | in_progress | done
session_id: <sid>
cwd: <abs path>
created: <iso8601>
last_resumed: <iso8601 or null>
completion_signal: auto-todowrite | auto-user-msg | auto-open-q | auto-default | manual | backfill-*
archive_hash: <memory doc hash>
---
```

Detector lives in `handoff/lifecycle.py:detect_status`. Precedence
(first match wins):

1. `auto-todowrite` — last TodoWrite call: all entries completed → `done`
2. `auto-user-msg`  — any of last 3 user msgs match completion regex → `done`
3. `auto-open-q`    — final user msg looks like a question → `pending`
4. `auto-default`   — fallback → `in_progress`

**Conservative bias:** uncertainty → `in_progress`, NEVER `done`.
False-`done` hides briefs from `/hand:on` (bad); false-`in_progress`
just clutters the picker (mild). Don't loosen the regex without strong
evidence.

`/hand:off` re-runs are idempotent: `created` and `last_resumed` are
preserved from the existing brief; `status` is re-detected unless the
current value is manual-`done` (then user wins).

Migration: `scripts/backfill_status.py` handles briefs without
frontmatter. It only reads the rendered brief body (no JSONL), so the
TodoWrite signal isn't available; backfill is conservative.

## Auto-stale sweep

`scripts/sweep_stale.py` flips `pending`/`in_progress` → `done` (signal
`auto-stale`) when the most-recent activity (`last_resumed` if set, else
`created`) is older than `--days` (default `STALE_DAYS_DEFAULT = 14`).
Manual statuses are never touched.

```bash
PYTHONPATH=. python3 scripts/sweep_stale.py              # dry-run
PYTHONPATH=. python3 scripts/sweep_stale.py --apply
PYTHONPATH=. python3 scripts/sweep_stale.py --days 30 --apply
```

Cron-able for hands-off hygiene:
```
0 6 * * *  cd ~/repos/handoff && PYTHONPATH=. python3 scripts/sweep_stale.py --apply
```

Reversible: `/hand:done <sid> --reopen` revives a brief with a manual
signal, which is sticky — auto-stale won't re-close it.

## Known follow-ups (not blocking)

- Hoist `compute_fixture_stats` so bench.py and render_html.py stop
  reimplementing the same per-fixture stats dict.
- Hoist a shared `_tool_result_text` helper used by both
  `extract_agent_reports` and `trim.build_convo`.
- Memoize `load_jsonl` + `render_brief` in render_html.py — currently
  parses + renders xhuge twice per run.
- Fold the 6-pass `stats_for` fan-out in bench.py into a single walk.

These are wallclock wins on `xhuge.jsonl` (86 MB). Cold paths, low
priority — only worth it if dev iteration on the report becomes painful.
