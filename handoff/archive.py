"""Trim a Claude Code transcript and store it as a memory doc.

The body sent to `memory doc store` is the output of `trim_transcript()`
(tool_result bodies + thinking blocks dropped), NOT the raw JSONL. This
keeps doc storage lean — full raw transcripts averaged ~4 MB each and
ballooned the memory DB before this change.

Writes a marker at ``~/.claude/memory/extracted/<session_id>.marker`` so
the SessionStart auto-archive scanner in memory v1.3.0+ skips the same
session on the next launch.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from handoff.transcript import trim_transcript

# A brief still in play must keep its archive: /hand:on can be run against it,
# and the brief's `archive_hash` is the only pointer to the full transcript.
# `done` and `archived` briefs are finished work, and a session row that no
# longer exists (the pre-1.0 auto-archive hook wrote archives for every session,
# with no brief at all) has nothing pointing at it.
OPEN_STATUSES = frozenset({"pending", "in_progress"})

DEFAULT_RETENTION_DAYS = 30

# `in_progress` is set by /hand:on and only cleared by /hand:done, and `pending`
# is just the auto-detected default — so neither decays on its own. Measured on
# the real index: 298 sessions sit at in_progress with the oldest from May, and
# 265 nominally-open briefs are over a month old. Treating "open" as permanent
# protection would pin 56% of archives forever, which is not a retention policy.
# An open brief keeps its archive until it has been untouched this long.
DEFAULT_OPEN_DAYS = 90

# Each `memory doc delete` is a cold CLI spawn, ~0.94s. The auto-prune runs on
# the /hand:off path, so it deletes at most this many per run and drains the
# backlog over several days rather than blocking one handoff for minutes.
# Steady state is ~12 archives/day, well under the cap.
AUTO_PRUNE_LIMIT = 50
AUTO_PRUNE_INTERVAL_SEC = 24 * 3600


def _marker_path(session_id: str) -> Path:
    return Path.home() / ".claude" / "memory" / "extracted" / f"{session_id}.marker"


def _memory_bin() -> str | None:
    """Resolve the `memory` CLI. It's a uv-tool install under ~/.local/bin,
    which isn't on the non-login-shell PATH this process usually runs under —
    so fall back to that known location before giving up."""
    return shutil.which("memory") or next(
        (str(p) for p in [Path.home() / ".local" / "bin" / "memory"] if p.exists()), None
    )


def archive_full_session(
    transcript_path: str,
    session_id: str,
    cwd: str,
    max_chars: int = 200_000,
) -> str | None:
    """Trim transcript → store as memory doc → write scanner marker.

    Returns the stored content_hash (or None on failure; errors logged to
    stderr). Body sent to memory is trimmed text, not raw JSONL.
    """
    project = os.path.basename(cwd) or "unknown"
    sid_short = session_id[:8]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        body = trim_transcript(transcript_path, max_chars=max_chars)
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"[archive] trim failed: {e}\n")
        return None

    if not body.strip():
        sys.stderr.write("[archive] trimmed transcript empty; skipping\n")
        return None

    title = f"Session {sid_short} {project} {date}"
    summary = body[:500] + ("…" if len(body) > 500 else "")
    tags = ["source:auto", "session-archive", f"project:{project}"]
    metadata = {"session_id": sid_short, "source_jsonl": transcript_path}

    mem = _memory_bin()
    if mem is None:
        sys.stderr.write("[archive] `memory` CLI not found (not on PATH, not in ~/.local/bin); skipping archive\n")
        return None

    # Pass the body via a temp file (--body-file): trimmed transcripts run to
    # hundreds of KB, past the argv size limit an inline --body would hit.
    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as f:
        f.write(body)
        body_file = f.name
    try:
        proc = subprocess.run(
            [mem, "doc", "store", "--title", title, "--summary", summary,
             "--body-file", body_file, "--type", "session-archive",
             "--tags", ",".join(tags), "--metadata", json.dumps(metadata)],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        sys.stderr.write(f"[archive] memory doc store failed to run: {e}\n")
        return None
    finally:
        os.unlink(body_file)

    if proc.returncode != 0:
        sys.stderr.write(f"[archive] memory doc store exited {proc.returncode}: {proc.stderr.strip()}\n")
        return None
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(f"[archive] could not parse memory output: {proc.stdout.strip()[:200]}\n")
        return None

    content_hash = result.get("content_hash")
    if content_hash:
        try:
            marker = _marker_path(session_id)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"archived\n{content_hash}\n", encoding="utf-8")
        except OSError as e:
            sys.stderr.write(f"[archive] marker write failed: {e}\n")
    return content_hash


def compute_body_hash(body: str) -> str:
    """sha256 of the trimmed body — useful for tests and dedup checks."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _open_archive_hashes(
    db_path: str | os.PathLike[str] | None = None,
    open_days: int = DEFAULT_OPEN_DAYS,
    now: float | None = None,
) -> set[str]:
    """Archive hashes of briefs that are open AND recently touched.

    Liveness is `last_resumed` when the brief has ever been resumed, otherwise
    `created`. A brief nobody has opened in `open_days` is abandoned rather than
    active, whatever its status column says.
    """
    from handoff import db as _db

    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    cutoff = now - open_days * 86400
    placeholders = ",".join("?" * len(OPEN_STATUSES))
    with _db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT archive_hash, created, last_resumed FROM sessions "
            f"WHERE archive_hash IS NOT NULL AND status IN ({placeholders})",
            tuple(sorted(OPEN_STATUSES)),
        ).fetchall()

    keep = set()
    for r in rows:
        touched = r["last_resumed"] or r["created"]
        try:
            ts = datetime.fromisoformat(str(touched).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            keep.add(r["archive_hash"])  # unreadable date is not evidence of abandonment
            continue
        if ts >= cutoff:
            keep.add(r["archive_hash"])
    return keep


def _list_archive_docs(mem: str, page_size: int = 200) -> list[dict]:
    """Every session-archive doc, bodies omitted.

    --depth summary matters here: a full listing of two archives is 142 KB
    against 2.5 KB, and there are hundreds of them.
    """
    docs: list[dict] = []
    page = 1
    while True:
        proc = subprocess.run(
            [mem, "doc", "list", "--type", "session-archive", "--depth", "summary",
             "--page", str(page), "--page-size", str(page_size)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            sys.stderr.write(f"[prune] doc list exited {proc.returncode}: {proc.stderr.strip()}\n")
            break
        try:
            batch = json.loads(proc.stdout).get("documents") or []
        except json.JSONDecodeError:
            sys.stderr.write("[prune] could not parse doc list output\n")
            break
        docs.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
    return docs


def prune_archives(
    days: int = DEFAULT_RETENTION_DAYS,
    dry_run: bool = False,
    db_path: str | os.PathLike[str] | None = None,
    limit: int | None = None,
    open_days: int = DEFAULT_OPEN_DAYS,
) -> dict:
    """Delete session-archive docs older than `days` whose brief is finished.

    /hand:off writes one archive per run — roughly a dozen a day — and nothing
    ever removed them, so the doc store had grown to 689 archives / 52.9 MB.
    Deleting today's pile without stopping the producer just refills it, which
    is why this runs on the producer's own path.

    `memory doc delete` is a soft delete with a 30-day purge window, so a
    mistake here is recoverable for a month.
    """
    mem = _memory_bin()
    if mem is None:
        return {"error": "memory CLI not found", "deleted": 0}

    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    try:
        keep = _open_archive_hashes(db_path, open_days=open_days)
    except Exception as e:  # sessions DB missing or unreadable — keep everything
        sys.stderr.write(f"[prune] cannot read sessions DB, skipping prune: {e}\n")
        return {"error": str(e), "deleted": 0}

    stats = {"scanned": 0, "kept_open": 0, "kept_recent": 0, "deleted": 0, "failed": 0}
    for doc in _list_archive_docs(mem):
        stats["scanned"] += 1
        h = doc.get("content_hash")
        if not h:
            continue
        if h in keep:
            stats["kept_open"] += 1
            continue
        try:
            created = datetime.fromisoformat(doc["created_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            # An unparseable timestamp is not evidence that the doc is old.
            stats["kept_recent"] += 1
            continue
        if created >= cutoff:
            stats["kept_recent"] += 1
            continue
        if limit is not None and stats["deleted"] >= limit:
            stats["deferred"] = stats.get("deferred", 0) + 1
            continue
        if dry_run:
            stats["deleted"] += 1
            continue
        proc = subprocess.run([mem, "doc", "delete", h], capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            stats["deleted"] += 1
        else:
            stats["failed"] += 1
    stats["days"] = days
    stats["open_days"] = open_days
    stats["dry_run"] = dry_run
    return stats


def _prune_stamp() -> Path:
    return Path.home() / ".claude" / "memory" / "state" / "prune-archives.stamp"


def maybe_prune_archives(now: float | None = None) -> dict | None:
    """Run the prune at most once a day, and never let it break a /hand:off.

    Returns the prune stats, or None when throttled or when anything went
    wrong — the archive has already been written by this point and losing it
    to a housekeeping error would be a bad trade.
    """
    stamp = _prune_stamp()
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    try:
        if stamp.exists() and now - stamp.stat().st_mtime < AUTO_PRUNE_INTERVAL_SEC:
            return None
    except OSError:
        return None
    try:
        stats = prune_archives(limit=AUTO_PRUNE_LIMIT)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps(stats), encoding="utf-8")
        return stats
    except Exception as e:  # housekeeping must never fail the handoff
        sys.stderr.write(f"[prune] skipped: {e}\n")
        return None
