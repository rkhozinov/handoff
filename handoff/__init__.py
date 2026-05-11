"""Deterministic transcript trimmer + memory archive for Claude Code sessions.

Replaces the lossy default `/compact` summarizer with a rule-based trim that
preserves user messages, code blocks, file paths, decisions, and errors
verbatim while dropping tool-result bodies and (optionally) middle-session
tool-narration turns.
"""
__version__ = "0.1.0"
