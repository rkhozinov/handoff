"""Tests for `handoff.sessionlog` — global sessions.log.md upsert."""
from __future__ import annotations

from pathlib import Path

from handoff.sessionlog import update_session_log

SID_A = "8e4178e1-7dc1-4352-b13e-98faeb5c1116"
SID_B = "23509468-67c8-49a9-8808-2574e03c88ce"


def _entry(log: Path, sid: str, **over) -> Path:
    kw = dict(
        session_id=sid,
        cwd="/Users/x/repos/infrastructure",
        status="in_progress",
        recap="Goal: x. Next: y.",
        created="2026-06-04T10:00:00Z",
    )
    kw.update(over)
    return update_session_log(log, **kw)


class TestUpdateSessionLog:
    def test_creates_file_and_parent(self, tmp_path: Path):
        log = tmp_path / "nested" / "sessions.log.md"
        _entry(log, SID_A)
        text = log.read_text()
        assert "## 2026-06-04 /Users/x/repos/infrastructure [in_progress]" in text
        assert "Goal: x. Next: y." in text

    def test_restore_line_has_full_sid(self, tmp_path: Path):
        log = tmp_path / "log.md"
        _entry(log, SID_A)
        assert f"restore: /hand:on {SID_A}" in log.read_text()

    def test_home_cwd_collapsed(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/x")
        log = tmp_path / "log.md"
        _entry(log, SID_A, cwd="/Users/x/repos/handoff")
        assert "## 2026-06-04 ~/repos/handoff [in_progress]" in log.read_text()

    def test_appends_second_sid(self, tmp_path: Path):
        log = tmp_path / "log.md"
        _entry(log, SID_A)
        _entry(log, SID_B, recap="other session")
        text = log.read_text()
        assert text.count("## ") == 2
        assert text.index(SID_A) < text.index(SID_B)

    def test_rerun_replaces_not_duplicates(self, tmp_path: Path):
        log = tmp_path / "log.md"
        _entry(log, SID_A, status="in_progress")
        _entry(log, SID_A, status="done", recap="Goal: x. Shipped.")
        text = log.read_text()
        assert text.count(SID_A) == 1
        assert "[done]" in text
        assert "Shipped." in text
        assert "Next: y." not in text

    def test_replace_keeps_other_entries(self, tmp_path: Path):
        log = tmp_path / "log.md"
        _entry(log, SID_A)
        _entry(log, SID_B, recap="other session")
        _entry(log, SID_A, status="done")
        text = log.read_text()
        assert text.count("## ") == 2
        assert "other session" in text

    def test_no_recap_still_has_restore_line(self, tmp_path: Path):
        log = tmp_path / "log.md"
        _entry(log, SID_A, recap=None)
        text = log.read_text()
        assert "[in_progress]" in text
        assert f"restore: /hand:on {SID_A}" in text

    def test_missing_created_renders_question_mark(self, tmp_path: Path):
        log = tmp_path / "log.md"
        _entry(log, SID_A, created=None)
        assert log.read_text().startswith("## ? ")

    def test_title_used_as_heading(self, tmp_path: Path):
        log = tmp_path / "log.md"
        _entry(log, SID_A, title="TICKET-445 app-api perf")
        text = log.read_text()
        assert "## 2026-06-04 TICKET-445 app-api perf [in_progress]" in text
        # cwd still present on the restore line
        assert f"restore: /hand:on {SID_A} (/Users/x/repos/infrastructure)" in text

    def test_no_title_falls_back_to_cwd(self, tmp_path: Path):
        log = tmp_path / "log.md"
        _entry(log, SID_A, title=None)
        assert "## 2026-06-04 /Users/x/repos/infrastructure [in_progress]" in log.read_text()

    def test_rerun_with_title_replaces_cwd_heading(self, tmp_path: Path):
        log = tmp_path / "log.md"
        _entry(log, SID_A)  # no title → cwd heading
        _entry(log, SID_A, title="real title")
        text = log.read_text()
        assert text.count("## ") == 1
        assert "real title" in text
