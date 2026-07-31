"""Blinding: provider identity and provenance shapes never reach a judge.

Three mechanical guarantees back the judge policy:

- :class:`BlindingMap` mints neutral ``system-A`` tokens per provider,
  deterministically from a run-scoped seed string via hashlib — never the
  ``random`` module (interpreter/wall-clock entropy would break run
  reproducibility).
- :func:`normalize_for_judge` rewrites provider-identifying shapes —
  ``exomem://…`` refs, ``[ref:SRC-…]`` sentinels, bare sentinel source ids,
  vault-path shapes (``Knowledge Base/...``, ``*.md`` paths), and product
  names — into neutral tokens. Source-like shapes become ``[ctx:N]`` with
  stable per-source numbering within one request (share one
  :class:`SourceNumbering` across all fields of a request).
- :func:`leakage_scan` is the hard gate: it flags residual identifying
  tokens, and a leaky request FAILS the judge phase (the handshake writer
  refuses to serialize it). The scan is deliberately broader than the
  rewriter — anything the rewriter cannot neutralize fails closed.

Order randomization uses :func:`deterministic_permutation`: a permutation
derived from a seed string via hashlib, reproducible forever for the same
seed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeVar

from membench.ids import SENTINEL_RE

_T = TypeVar("_T")

#: Replacement for product-name mentions; products are not sources, so they
#: get one fixed neutral token rather than a numbered context token.
NEUTRAL_SYSTEM_TOKEN = "[system]"

_EXOMEM_URI_RE = re.compile(r"exomem://[^\s\"'\)\]>]+", re.IGNORECASE)
_BARE_SOURCE_ID_RE = re.compile(r"\bSRC-[A-Z0-9][A-Z0-9-]*\b")
_VAULT_PATH_RE = re.compile(r"Knowledge[ _-]Base/[^\s\"'\)\]>,;]*", re.IGNORECASE)
_MD_PATH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\\/-]*\.md\b")
_PRODUCT_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"exomem", re.IGNORECASE),
    re.compile(r"basic[ _-]?memory", re.IGNORECASE),
    re.compile(r"graybox", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])mem0(?![A-Za-z0-9])", re.IGNORECASE),
)

# leakage_scan patterns: broader than the rewriter on purpose (fail closed).
_LEAK_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("exomem_uri", re.compile(r"exomem://", re.IGNORECASE)),
    ("sentinel", re.compile(r"\[ref:", re.IGNORECASE)),
    ("source_id", _BARE_SOURCE_ID_RE),
    ("vault_path", re.compile(r"Knowledge[ _-]Base/", re.IGNORECASE)),
    ("md_path", re.compile(r"\.md\b", re.IGNORECASE)),
    ("product", _PRODUCT_RES[0]),
    ("product", _PRODUCT_RES[1]),
    ("product", _PRODUCT_RES[2]),
    ("product", _PRODUCT_RES[3]),
)


def deterministic_permutation(count: int, seed: str) -> list[int]:
    """Permutation of ``range(count)`` derived from ``seed`` via hashlib.

    Sort-by-digest: each index is keyed by ``sha256(seed + ":" + index)``,
    so the order is fully determined by the seed string — no ``random``
    module, no wall clock.
    """

    if count < 0:
        raise ValueError("count must be >= 0")
    return sorted(
        range(count),
        key=lambda index: hashlib.sha256(f"{seed}:{index}".encode()).hexdigest(),
    )


def shuffled(items: Sequence[_T], seed: str) -> list[_T]:
    """Deterministically reordered copy of ``items`` (see permutation notes)."""

    return [items[index] for index in deterministic_permutation(len(items), seed)]


def _spread_letters(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA … (unbounded, deterministic)."""

    chars: list[str] = []
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        chars.append(chr(ord("A") + remainder))
    return "".join(reversed(chars))


@dataclass(frozen=True)
class BlindingMap:
    """Provider → neutral token map, minted deterministically per run.

    Token assignment order is a seed-derived permutation of the sorted
    provider list, so which provider becomes ``system-A`` changes with the
    run-scoped seed but never within a run.
    """

    seed: str
    tokens: MappingProxyType

    @classmethod
    def mint(cls, providers: Sequence[str], seed: str) -> BlindingMap:
        unique = sorted(set(providers))
        ordered = shuffled(unique, f"{seed}:provider-order")
        mapping = {
            provider: f"system-{_spread_letters(position)}"
            for position, provider in enumerate(ordered)
        }
        return cls(seed=seed, tokens=MappingProxyType(mapping))

    def token_for(self, provider: str) -> str:
        try:
            return self.tokens[provider]
        except KeyError:
            raise KeyError(f"provider {provider!r} not in blinding map") from None

    def as_dict(self) -> dict[str, str]:
        return dict(self.tokens)


class SourceNumbering:
    """Stable per-source ``[ctx:N]`` numbering within ONE judge request.

    Share one instance across every field of a request so the same source
    keeps the same number; use a fresh instance per request so numbering
    never carries cross-request information.
    """

    def __init__(self) -> None:
        self._numbers: dict[str, int] = {}

    def token(self, key: str) -> str:
        number = self._numbers.get(key)
        if number is None:
            number = len(self._numbers) + 1
            self._numbers[key] = number
        return f"[ctx:{number}]"


def normalize_for_judge(text: str, numbering: SourceNumbering | None = None) -> str:
    """Rewrite provider-identifying shapes in ``text`` into neutral tokens.

    Source-like shapes (refs, sentinels, vault paths) become ``[ctx:N]``;
    a sentinel and a bare mention of the same source id share one N.
    Product names become :data:`NEUTRAL_SYSTEM_TOKEN`.
    """

    numbers = numbering if numbering is not None else SourceNumbering()

    def _ctx_whole(match: re.Match[str]) -> str:
        return numbers.token(match.group(0))

    def _ctx_sentinel(match: re.Match[str]) -> str:
        return numbers.token(match.group(1))

    out = _EXOMEM_URI_RE.sub(_ctx_whole, text)
    out = SENTINEL_RE.sub(_ctx_sentinel, out)
    out = _VAULT_PATH_RE.sub(_ctx_whole, out)
    out = _MD_PATH_RE.sub(_ctx_whole, out)
    out = _BARE_SOURCE_ID_RE.sub(_ctx_whole, out)  # after sentinels: ids share their N
    for pattern in _PRODUCT_RES:
        out = pattern.sub(NEUTRAL_SYSTEM_TOKEN, out)
    return out


def leakage_scan(text: str) -> list[str]:
    """Residual provider-identifying tokens in ``text`` (empty = clean).

    Used as a hard gate by the handshake writer: any hit means the request
    is refused, never written. Matches are returned verbatim (deduplicated,
    order of first appearance) so refusals are explainable.
    """

    found: dict[str, None] = {}
    for _kind, pattern in _LEAK_RES:
        for match in pattern.finditer(text):
            found.setdefault(match.group(0))
    return list(found)
