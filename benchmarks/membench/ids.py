"""Deterministic identifiers, seeds, and sentinel citation tokens."""

from __future__ import annotations

import hashlib
import re

SENTINEL_RE = re.compile(r"\[ref:([A-Z0-9][A-Z0-9-]*)\]")


def _digest(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    """Deterministic id such as ``SRC-T01-1A2B3C4D`` from stable parts."""

    return f"{prefix}-{_digest(prefix, *parts)[:8].upper()}"


def child_seed(master_seed: int, template_id: str, variant: int) -> int:
    """Independent per-(template, variant) seed; template isolation by construction."""

    return int(_digest(str(master_seed), template_id, str(variant))[:16], 16)


def sentinel(source_id: str) -> str:
    """Visible citation token embedded in source bodies: ``[ref:<SOURCE-ID>]``."""

    return f"[ref:{source_id}]"


def sentinels_in(text: str) -> list[str]:
    """All sentinel source ids present in ``text`` (order preserved, deduped)."""

    seen: dict[str, None] = {}
    for match in SENTINEL_RE.finditer(text):
        seen.setdefault(match.group(1))
    return list(seen)


def slugify(text: str) -> str:
    """Lowercase ASCII kebab slug for filenames (deterministic, dependency-free)."""

    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return cleaned.strip("-") or "item"
