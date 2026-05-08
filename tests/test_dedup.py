"""Tests for compaction.dedup.

`semantic_dedup` requires `model2vec` + `numpy`. When either is missing the
function is a no-op. Tests that exercise the actual embedding path are
gated with `pytest.importorskip` so CI w/o the optional deps still passes.
"""
from __future__ import annotations

import builtins
import sys

import pytest

from compaction import dedup
from compaction.dedup import semantic_dedup


# ---------------------------------------------------------- no-op fallbacks ----


def test_empty_convo_passthrough() -> None:
    out, dropped = semantic_dedup([])
    assert out == []
    assert dropped == 0


def test_single_turn_passthrough() -> None:
    convo = [("user", "hello world")]
    out, dropped = semantic_dedup(convo)
    assert out == convo
    assert dropped == 0


def test_no_op_when_model2vec_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dedup should be a clean no-op when model2vec import fails — the
    core trimmer must not depend on the optional embedding stack."""
    # Reset module-global state so the failed import is observed.
    monkeypatch.setattr(dedup, "_MODEL", None)
    monkeypatch.setattr(dedup, "_LOAD_FAILED", False)

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "model2vec" or name.startswith("model2vec."):
            raise ImportError("simulated: model2vec not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Drop any cached module so the lazy import re-runs.
    monkeypatch.setitem(sys.modules, "model2vec", None)

    convo = [("user", "x" * 100), ("user", "x" * 100)]
    out, dropped = semantic_dedup(convo)
    assert out == convo
    assert dropped == 0


# -------------------------------------------------------------- real path ----


def test_paraphrased_adjacent_assistant_dropped() -> None:
    """When the embedding stack is available, two near-paraphrased adjacent
    assistant turns should collapse to one. (Retry-with-suffix pattern is
    one of the highest-similarity real-world cases — ~0.95.)"""
    pytest.importorskip("model2vec")
    pytest.importorskip("numpy")
    convo = [
        ("assistant", "error: connection refused on port 5432 from postgres client"),
        ("assistant", "error: connection refused on port 5432 from postgres client (retry 1)"),
    ]
    out, dropped = semantic_dedup(convo, threshold=0.95)
    assert dropped == 1
    assert len(out) == 1


def test_user_turns_never_dropped() -> None:
    """User turns are pure signal — even near-identical pairs must survive.
    This pins down the invariant the bench guards (signal_kept == user_signal)."""
    pytest.importorskip("model2vec")
    pytest.importorskip("numpy")
    convo = [
        ("user", "please run the kubectl command to list all pods in production"),
        ("user", "please run the kubectl command to list all pods in production again"),
    ]
    out, dropped = semantic_dedup(convo, threshold=0.5)
    assert dropped == 0
    assert len(out) == 2


def test_distinct_turns_preserved() -> None:
    """Two semantically different turns must NOT collapse."""
    pytest.importorskip("model2vec")
    pytest.importorskip("numpy")
    convo = [
        ("assistant", "[Bash kubectl get pods -n production --selector app=api]"),
        ("assistant", "[Read /Users/me/repo/src/main.py]"),
    ]
    out, dropped = semantic_dedup(convo, threshold=0.90)
    assert dropped == 0
    assert len(out) == 2


def test_short_turns_passthrough() -> None:
    """Turns under MIN_LEN bypass dedup — static embeddings are too noisy
    on short strings (kubectl `-n prod` vs `-n stage` is ~0.93)."""
    pytest.importorskip("model2vec")
    pytest.importorskip("numpy")
    convo = [
        ("assistant", "[Bash ls -la]"),
        ("assistant", "[Bash ls -la]"),
    ]
    out, dropped = semantic_dedup(convo, threshold=0.5)
    # Both turns are under 30 chars → bypass entirely
    assert dropped == 0
    assert len(out) == 2


def test_different_roles_never_collapse() -> None:
    """Even with identical text across roles, dedup must not cross role
    boundaries — user request vs assistant echo are different events."""
    pytest.importorskip("model2vec")
    pytest.importorskip("numpy")
    text = "we need to refactor the authentication middleware to validate tokens"
    convo = [("user", text), ("assistant", text)]
    out, dropped = semantic_dedup(convo, threshold=0.5)
    assert dropped == 0
    assert len(out) == 2


def test_high_threshold_keeps_paraphrase() -> None:
    """Threshold knob: at 0.99 even close paraphrases survive."""
    pytest.importorskip("model2vec")
    pytest.importorskip("numpy")
    convo = [
        ("assistant", "error: connection refused on port 5432 from postgres client"),
        ("assistant", "error: connection refused on port 5432 from postgres client (retry 1)"),
    ]
    out, dropped = semantic_dedup(convo, threshold=0.99)
    assert dropped == 0
    assert len(out) == 2
