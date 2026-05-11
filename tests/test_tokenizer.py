"""Tests for handoff.tokenizer.

The tokenizer module exposes one canonical entry point — `count_tokens(text,
mode=...)`. These tests pin down each mode's contract without requiring any
optional deps in CI: the `hf` test self-skips when transformers isn't
installed, and the `api` test verifies we error cleanly without making a
network call.
"""
from __future__ import annotations

import builtins
import sys

import pytest

from handoff import tokenizer
from handoff.tokenizer import VALID_MODES, count_tokens


# ---------------------------------------------------------------- chars4 ----

def test_chars4_returns_byte_div_4() -> None:
    """chars4 is byte-len // 4 — exactly the legacy heuristic."""
    text = "hello world"  # 11 utf-8 bytes
    assert count_tokens(text, mode="chars4") == 11 // 4


def test_chars4_empty_string_is_zero() -> None:
    assert count_tokens("", mode="chars4") == 0


def test_chars4_handles_unicode_bytes_not_chars() -> None:
    """Multi-byte chars must count by bytes (matches len(text.encode))."""
    # "é" is 2 utf-8 bytes; "中" is 3 utf-8 bytes.
    text = "é中"  # 5 bytes => 1 token
    assert count_tokens(text, mode="chars4") == 5 // 4


def test_chars4_deterministic_across_calls() -> None:
    s = "the quick brown fox jumps over the lazy dog"
    assert count_tokens(s, mode="chars4") == count_tokens(s, mode="chars4")


# ----------------------------------------------------------------- auto ----

def test_auto_returns_positive_for_nonempty() -> None:
    assert count_tokens("hello world", mode="auto") > 0


def test_auto_default_mode_is_auto() -> None:
    """Calling without mode= must equal mode='auto'."""
    s = "default mode probe"
    assert count_tokens(s) == count_tokens(s, mode="auto")


def test_auto_falls_back_to_chars4_when_transformers_missing(monkeypatch) -> None:
    """When `transformers` cannot be imported, `auto` must equal `chars4`."""
    # Wipe any cached HF tokenizer from a prior test run.
    tokenizer._HF_TOKENIZERS.clear()

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "transformers" or name.startswith("transformers."):
            raise ImportError("simulated: transformers not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Also evict any already-imported transformers modules so the lazy import
    # actually re-runs and hits our fake.
    for mod in [m for m in sys.modules if m == "transformers" or m.startswith("transformers.")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)

    text = "hello world"
    assert count_tokens(text, mode="auto") == count_tokens(text, mode="chars4")


# -------------------------------------------------------------------- hf ----

def test_hf_mode_against_xenova_tokenizer() -> None:
    """When transformers is available, hf returns a positive integer.

    Skipped on environments without `transformers` installed (CI default)
    so the rest of the suite stays dependency-free.
    """
    pytest.importorskip("transformers")
    n = count_tokens("hello world", mode="hf")
    assert isinstance(n, int)
    assert n > 0


# ------------------------------------------------------------------- api ----

def test_api_mode_raises_cleanly_without_key(monkeypatch) -> None:
    """No ANTHROPIC_API_KEY => RuntimeError with a clear message, no network."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # If `anthropic` *is* installed in the env, make sure the test still
    # never instantiates a client by stubbing out the SDK before the call.
    fake_module = type(sys)("anthropic")

    class _Boom:
        def __init__(self, *_args, **_kwargs):  # pragma: no cover
            raise AssertionError("anthropic.Anthropic must not be constructed")

    fake_module.Anthropic = _Boom
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        count_tokens("hello", mode="api")


def test_api_mode_raises_when_sdk_missing(monkeypatch) -> None:
    """SDK not importable => RuntimeError mentioning the `anthropic` package."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-not-used")

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "anthropic" or name.startswith("anthropic."):
            raise ImportError("simulated: anthropic not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    for mod in [m for m in sys.modules if m == "anthropic" or m.startswith("anthropic.")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    # Clear any cached client keyed by a stale key.
    tokenizer._ANTHROPIC_CLIENTS.clear()

    with pytest.raises(RuntimeError, match="anthropic"):
        count_tokens("hello", mode="api")


# --------------------------------------------------------------- misc ----

def test_unknown_mode_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown token-count mode"):
        count_tokens("x", mode="bogus")


def test_valid_modes_includes_all_four() -> None:
    assert set(VALID_MODES) == {"auto", "hf", "api", "chars4"}


def test_empty_text_returns_zero_for_every_mode(monkeypatch) -> None:
    """Empty input is short-circuited before any backend is consulted."""
    # Disable the API key so 'api' would fail loudly if it were invoked —
    # we want to prove count_tokens("") returns 0 *without* dispatching.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for m in VALID_MODES:
        assert count_tokens("", mode=m) == 0
