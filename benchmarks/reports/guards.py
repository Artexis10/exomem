"""Shared refusals every reports renderer applies before returning text."""

from __future__ import annotations


class ReportRefused(ValueError):
    """A renderer refused to publish text that violates a report invariant."""


def refuse_aggregate(text: str) -> str:
    """Return ``text`` unchanged, or refuse it if it carries an aggregate score."""
    if "aggregate" in text.lower():
        raise ReportRefused("reports never publish an aggregate; refusing to render")
    return text
