"""scripts/import_onhold.py — dev script, not shipped by the plugin.

One-off migration of a hand-kept "come back later" list (`~/todo-onhold`,
lines of the form `claude --resume "name"` or `claude --resume <uuid>`,
optionally with `( /hand:on <uuid> )` and/or a `from dir: /path` suffix)
into real on_hold briefs. Safe to re-run — an existing brief is kept as-is
and re-holding the same session is idempotent.

Measured facts (2026-09-21, real `~/.claude/projects`) driving the design:
- 36 lines, every one starts `claude --resume`; 17 use a quoted name.
- A name resolves via `{"type":"custom-title","customTitle":"<name>",
  "sessionId":"<uuid>"}` entries; `grep -l` over all transcripts is ~0.16s.
- 6 of 17 names map to 2-3 session ids (a resumed/cleared session rewrites
  the custom-title with its own sessionId). Resolution order: an explicit
  `/hand:on <uuid>` on the line wins; else the transcript with the newest
  mtime; the rest are reported as `others`.
- Every transcript entry carries `cwd`; `cwd_from_entries` already extracts
  it, loaded LAZILY (only for the chosen candidate — some transcripts are
  86 MB).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handoff import cli, db, dbcli, tasks
from handoff.extract import cwd_from_entries, load_jsonl

UUID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")

DEFAULT_TODO = Path("~/todo-onhold")
DEFAULT_PROJECTS_DIR = Path("~/.claude/projects")
DEFAULT_COMPACTION_DIR = Path("~/.claude/compaction")


@dataclass
class Line:
    raw: str
    key: str
    explicit_sid: str | None


def parse_line(line: str) -> Line | None:
    raw = line.rstrip("\n")
    s = raw.strip()
    if not s or s.startswith("#") or not s.startswith("claude --resume"):
        return None
    rest = s[len("claude --resume"):].strip()
    if rest.startswith('"'):
        end = rest.find('"', 1)
        key = rest[1:end] if end != -1 else rest[1:]
    else:
        key = rest.split()[0] if rest.split() else ""

    explicit_sid = None
    marker = "/hand:on"
    idx = s.find(marker)
    if idx != -1:
        m = UUID_RE.search(s[idx + len(marker):])
        if m:
            explicit_sid = m.group(1)

    return Line(raw=raw, key=key, explicit_sid=explicit_sid)


@dataclass
class Candidate:
    sid: str
    path: Path
    mtime: float
    cwd: str | None = None


def build_index(projects_dir: Path) -> dict[str, list[Candidate]]:
    """Index by custom-title -> candidates, plus sid -> [candidate] under the
    sid's own uuid key (so a uuid key resolves without a title)."""
    index: dict[str, list[Candidate]] = {}
    for path in sorted(projects_dir.glob("*/*.jsonl")):
        sid = path.stem
        mtime = path.stat().st_mtime
        cand = Candidate(sid=sid, path=path, mtime=mtime)
        index.setdefault(sid, []).append(cand)

        title = None
        for text_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if '"type":"custom-title"' not in text_line and '"type": "custom-title"' not in text_line:
                continue
            try:
                entry = json.loads(text_line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "custom-title":
                title = entry.get("customTitle")
        if title:
            index.setdefault(title, []).append(cand)
    return index


@dataclass
class Resolved:
    line: Line
    sid: str | None
    cwd: str | None = None
    path: Path | None = None
    others: list[str] = field(default_factory=list)
    reason: str | None = None
    snapshot: str | None = None

    @property
    def key(self) -> str:
        return self.line.key


def resolve(line: Line, index: dict[str, list[Candidate]]) -> Resolved:
    if line.explicit_sid is not None:
        cands = index.get(line.explicit_sid)
        if not cands:
            return Resolved(line=line, sid=None, reason="explicit-sid-has-no-transcript")
        chosen = cands[0]
        return _finish(line, chosen, others=[])

    if UUID_RE.fullmatch(line.key):
        cands = index.get(line.key)
        if not cands:
            return Resolved(line=line, sid=None, reason="no-transcript-for-sid")
        chosen = cands[0]
        return _finish(line, chosen, others=[])

    cands = index.get(line.key)
    if not cands:
        return Resolved(line=line, sid=None, reason="no-transcript-with-that-title")
    ordered = sorted(cands, key=lambda c: c.mtime, reverse=True)
    chosen = ordered[0]
    others = [c.sid for c in ordered[1:]]
    return _finish(line, chosen, others=others)


def _finish(line: Line, chosen: Candidate, *, others: list[str]) -> Resolved:
    cwd = chosen.cwd
    if cwd is None:
        cwd = cwd_from_entries(load_jsonl(str(chosen.path)))
    return Resolved(line=line, sid=chosen.sid, cwd=cwd, path=chosen.path, others=others)


@dataclass
class Report:
    held: list[Resolved]
    unresolved: list[Resolved]


def run(
    todo_path: Path,
    *,
    projects_dir: Path,
    compaction_dir: Path,
    db_path: Path | None,
    dry_run: bool,
    archive: bool = True,
) -> Report:
    index = build_index(projects_dir)
    held: list[Resolved] = []
    unresolved: list[Resolved] = []

    for raw_line in todo_path.read_text(encoding="utf-8").splitlines():
        line = parse_line(raw_line)
        if line is None:
            continue
        r = resolve(line, index)
        if r.sid is None:
            unresolved.append(r)
            continue

        if dry_run:
            r.snapshot = "dry-run"
            held.append(r)
            continue

        brief_path = compaction_dir / f"{r.sid}.md"
        if brief_path.is_file():
            r.snapshot = "kept-existing"
        else:
            argv = [
                "--transcript", str(r.path),
                "--session-id", r.sid,
                "--cwd", r.cwd or "/",
                "--out-dir", str(compaction_dir),
                "--no-agent-store",
            ]
            if not archive:
                argv.append("--no-archive")
            if db_path is not None:
                argv.extend(["--db", str(db_path)])
            rc = cli.main(argv)
            if rc != 0:
                r.reason = f"handoff.cli rc={rc}"
                r.sid = None
                unresolved.append(r)
                continue
            r.snapshot = "created"
            try:
                dbcli.do_tasks_export(
                    r.sid,
                    tasks_root=tasks.DEFAULT_TASKS_DIR,
                    bundle_dir=tasks.DEFAULT_BUNDLE_DIR,
                    out=None,
                    list_id=None,
                )
            except Exception:
                pass

        dbcli.do_hold(
            r.sid, note=line.raw, until=None, release=False,
            compaction_dir=str(compaction_dir), db_path=db_path,
        )
        held.append(r)

    return Report(held=held, unresolved=unresolved)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("todo", nargs="?", default=str(DEFAULT_TODO), help="Hand-kept resume list")
    ap.add_argument("--projects-dir", default=str(DEFAULT_PROJECTS_DIR))
    ap.add_argument("--dir", default=str(DEFAULT_COMPACTION_DIR), help="Compaction/brief dir")
    ap.add_argument("--db", default=None, help="sessions.db path override")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-archive", action="store_true")
    args = ap.parse_args(argv)

    todo_path = Path(args.todo).expanduser()
    projects_dir = Path(args.projects_dir).expanduser()
    compaction_dir = Path(args.dir).expanduser()
    db_path = Path(args.db).expanduser() if args.db else None

    report = run(
        todo_path,
        projects_dir=projects_dir,
        compaction_dir=compaction_dir,
        db_path=db_path,
        dry_run=args.dry_run,
        archive=not args.no_archive,
    )

    for r in report.held:
        others = f" others: {','.join(o[:8] for o in r.others)}" if r.others else ""
        print(f"HELD {r.sid[:8]} {r.snapshot} cwd={r.cwd}{others} {r.key}")

    print("UNRESOLVED:")
    for r in report.unresolved:
        print(f"{r.reason}  {r.line.raw}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
