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
  tokens AND residual identifying *structure*, and a leaky request FAILS the
  judge phase (the handshake writer refuses to serialize it). The scan is
  deliberately broader than the rewriter — anything the rewriter cannot
  neutralize fails closed.

The structural axis exists because token blinding alone loses to schema
shape (task 4b.18): a frontmatter key run such as ``type / exomem_id /
title / source_type / captured / tags / ingested_into`` names the product
outright while every value is neutral and the token scan returns zero hits,
and scrubbing the product name out of the key leaves ``[system]_id:`` —
a residue that advertises a product-named field was removed. So
:func:`normalize_for_judge` canonicalizes any registered vendor key run to
one fixed neutral block BEFORE token rewriting, and
:func:`structural_leakage_scan` flags any that survives. The failure
predicate is byte identity on that axis: one semantic payload rendered with
two providers' native FRONTMATTER KEY RUNS must reach the judge as identical
frontmatter bytes, which is stronger than "no sampled classifier told them
apart".

The axis is the key run, and the remainder is named rather than implied: body
scaffolding is a second structural tell this module does NOT normalize. The
two producers wrap the same payload in different section headers
(`src/exomem/add.py` writes ``## Capture``,
`benchmarks/membench/native/basic_memory.py` writes ``## Observations``), so a
whole-document byte-identity claim would be false. Normalizing prose headings
is a separate scope decision, not an oversight here — the same way
:func:`_key_runs` names its column-0 block-list limit instead of hiding it.

Order randomization uses :func:`deterministic_permutation`: a permutation
derived from a seed string via hashlib, reproducible forever for the same
seed.
"""

from __future__ import annotations

import hashlib
import json
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

#: Fence line of a YAML frontmatter block.
_FRONTMATTER_FENCE = "---"

#: The token scrubber rewrites ``exomem_id:`` to this; the key shape survives
#: even though the product name does not, so the residue is its own signature.
_SCRUBBED_ID_KEY = f"{NEUTRAL_SYSTEM_TOKEN}_id"

#: A header line: ``key: value``, ``key:``, or the scrubbed-id residue key.
#: ``key:value`` with no separating space is deliberately NOT matched: it is
#: not valid YAML block-mapping syntax and neither registered producer emits
#: it, so matching it would only widen the false-positive surface.
_KEY_LINE_RE = re.compile(
    r"^[ \t]*("
    + re.escape(_SCRUBBED_ID_KEY)
    + r"|[A-Za-z_][A-Za-z0-9_.-]*)[ \t]*:(?:[ \t].*)?$"
)

#: Registered vendor structural signatures: a header-line run whose key set is
#: a superset of one of these identifies its producer with zero token hits.
#: Both are read off the producers that actually emit them in this repo —
#: `src/exomem/add.py` (the source-page writer) and
#: `benchmarks/membench/native/basic_memory.py` (note frontmatter) — never an
#: invented convenience shape.
_STRUCTURAL_SIGNATURES: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "exomem-source-frontmatter",
        frozenset({"type", "source_type", "captured", "ingested_into"}),
    ),
    ("exomem-id-key", frozenset({"exomem_id"})),
    ("system-id-residue", frozenset({_SCRUBBED_ID_KEY})),
    (
        "basic-memory-note-frontmatter",
        frozenset({"title", "type", "permalink", "tags"}),
    ),
)

#: Marker prefix for a structural hit in :func:`leakage_scan` output. Token
#: hits are returned verbatim; a structure has no single verbatim token to
#: return, so it is named instead — refusals stay explainable either way.
STRUCTURE_LEAK_PREFIX = "structure:"

#: Keys carried through structural canonicalization, in this fixed order.
#: ``title`` is the only frontmatter field with content a judge grades; every
#: other key is provenance or identity, which is exactly what blinding removes.
CANONICAL_FRONTMATTER_KEYS: tuple[str, ...] = ("title",)

#: Line breaks as the SCAN sees them. `write_requests` scans one serialized
#: JSON line in which every document newline is the two-character escape
#: ``\\n``, so a scan that only understood real newlines would find no key run
#: in the exact bytes the gate binds on. Longest alternatives first.
_SCAN_LINE_SPLIT_RE = re.compile(r"\\r\\n|\\n|\r\n|\n|\r")

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


def _fenced_run(
    lines: Sequence[str], opening: int
) -> tuple[int, int, dict[str, str]] | None:
    """The run delimited by a ``---`` fence pair at ``opening``, or ``None``.

    Inside a fence the run is bounded by the FENCE, not by the first non-key
    line, because a producer can emit a value that is not a single line. Basic
    Memory writes ``f"title: {source.title}"`` unescaped
    (`native/basic_memory.py:158`) over a ``SourceRecord.title`` that is an
    unconstrained ``str`` (`membench/schema.py:229`), so a title carrying a
    newline splits the block and — under a consecutive-line rule — would let
    ``permalink:`` walk past the gate into judge-visible text.

    Acceptance is deliberately narrow, because a false positive here DELETES
    content rather than merely refusing it: the interior must hold at least two
    key lines and strictly more key lines than other non-empty lines. Prose
    between two thematic breaks fails that; a frontmatter block with a
    line-broken value passes it. The returned span INCLUDES both fences.
    The residual is the mirror image of the rule: a value carrying at least
    as many line breaks as the block has keys makes ``other_lines`` catch up
    with ``key_lines``, the fenced run is rejected, the consecutive rule takes
    over, and ``permalink:`` can walk past the gate again. Neither registered
    producer emits such a value; it is named here, like the column-0 and
    ``key:value`` limits, rather than hidden.
    """

    closing = None
    for index in range(opening + 1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_FENCE:
            closing = index
            break
    if closing is None:
        return None
    keys: dict[str, str] = {}
    key_lines = 0
    other_lines = 0
    for line in lines[opening + 1 : closing]:
        match = _KEY_LINE_RE.match(line)
        if match is not None:
            key_lines += 1
            keys.setdefault(match.group(1), line.partition(":")[2])
        elif line.strip():
            other_lines += 1
    if key_lines < 2 or other_lines >= key_lines:
        return None
    return (opening, closing + 1, keys)


def _unfenced_run(
    lines: Sequence[str], start: int, floor: int
) -> tuple[int, int, dict[str, str]] | None:
    """The maximal run of consecutive header lines at ``start``, or ``None``.

    An indented non-empty line (a YAML block value such as a ``- item`` under
    its key) continues a run without contributing a key; anything else ends it.
    A block list written at column 0 therefore splits an UNFENCED run — a known
    limit, and neither registered producer writes one outside a fence. A ``---``
    immediately on either side is absorbed into the span, never below ``floor``
    (which is where the previous run ended), so spans never overlap.
    """

    index = start
    keys: dict[str, str] = {}
    while index < len(lines):
        line = lines[index]
        match = _KEY_LINE_RE.match(line)
        if match is not None:
            keys.setdefault(match.group(1), line.partition(":")[2])
            index += 1
            continue
        if keys and line[:1] in (" ", "\t") and line.strip():
            index += 1  # block value continuation; contributes no key
            continue
        break
    if not keys:
        return None
    first, stop = start, index
    if first - 1 >= floor and lines[first - 1].strip() == _FRONTMATTER_FENCE:
        first -= 1
    if stop < len(lines) and lines[stop].strip() == _FRONTMATTER_FENCE:
        stop += 1
    return (first, stop, keys)


def _key_runs(lines: Sequence[str]) -> list[tuple[int, int, dict[str, str]]]:
    """Header-line runs, as ``(start, stop, keys)`` in increasing start order.

    ``stop`` is exclusive and the span is what a caller should REPLACE: any
    ``---`` fence belonging to the run is already inside it. ``keys`` maps
    key → raw value text in first-seen order. A fenced block is preferred over
    the consecutive-line reading whenever it qualifies (see :func:`_fenced_run`).
    """

    runs: list[tuple[int, int, dict[str, str]]] = []
    index = 0
    floor = 0
    while index < len(lines):
        run = None
        if lines[index].strip() == _FRONTMATTER_FENCE:
            run = _fenced_run(lines, index)
        if run is None:
            run = _unfenced_run(lines, index, floor)
        if run is None:
            index += 1
            continue
        runs.append(run)
        index = floor = run[1]
    return runs


def _matched_signatures(present: set[str]) -> list[str]:
    """Names of the registered vendor signatures satisfied by ``present``."""

    return [name for name, required in _STRUCTURAL_SIGNATURES if required <= present]


def _unwrap_once(value: str) -> str:
    """One layer of quoting removed, or ``value`` unchanged if it carries none."""

    if len(value) < 2 or value[0] != value[-1] or value[0] not in "\"'":
        return value
    if value[0] == '"':
        try:
            decoded = json.loads(value)
        except ValueError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else value[1:-1]
    return value[1:-1]


def _canonical_value(raw: str) -> str:
    """Value text with producer-specific quoting removed, to a FIXED POINT.

    One producer renders scalars through ``json.dumps``
    (``src/exomem/vault.py`` ``yaml_scalar``) while the other writes them bare,
    so one title can arrive as ``"A: B"`` and as ``A: B``. Quoting is shape,
    not content, and byte identity has to survive it.

    Unwrapping ONCE is not enough, and the difference is a one-way tell: a
    title that itself carries quotes gets double-wrapped by ``yaml_scalar``
    (``"Quarterly"`` becomes ``"\\"Quarterly\\""``) while the other producer
    writes it bare, so a single unwrap leaves the first still quoted and the
    second bare. Iterating to a fixed point closes it. Each pass removes at
    least two characters, so the loop terminates; a value whose quotes are
    content rather than shape (``He said "go": deadline moved``) is not
    surrounded by a matched pair and is returned untouched.
    """

    value = raw.strip()
    while True:
        unwrapped = _unwrap_once(value)
        if unwrapped == value:
            return value
        value = unwrapped


def _canonical_block(keys: dict[str, str]) -> list[str]:
    """The one neutral block every registered vendor shape collapses to.

    Fixed key set, fixed order, fixed delimiter — so two native shapes of one
    payload become the same bytes. An empty carry means the block held nothing
    but provenance, and it disappears entirely rather than leaving a husk.
    """

    carried = [
        f"{key}: {_canonical_value(keys[key])}"
        for key in CANONICAL_FRONTMATTER_KEYS
        if _canonical_value(keys.get(key, ""))
    ]
    if not carried:
        return []
    return [_FRONTMATTER_FENCE, *carried, _FRONTMATTER_FENCE]


def _canonicalize_structure(text: str) -> str:
    """Rewrite every registered vendor key run to the one neutral block.

    Real newlines only: this REWRITES text, so it must not treat a literal
    backslash-n inside content as a line break the way the scan deliberately
    does. Runs already carry their own fences in their span, and are replaced
    back-to-front so earlier indices stay valid.
    """

    lines = text.split("\n")
    out = list(lines)
    for start, stop, keys in reversed(_key_runs(lines)):
        if not _matched_signatures(set(keys)):
            continue
        out[start:stop] = _canonical_block(keys)
    return "\n".join(out)


def structural_leakage_scan(text: str) -> list[str]:
    """Registered vendor structural signatures still present in ``text``.

    Returns ``structure:<signature>`` markers (empty = clean). Splitting is
    escape-tolerant on purpose: the gate binds on one serialized JSON line
    whose document newlines are two-character escapes.
    """

    found: dict[str, None] = {}
    lines = _SCAN_LINE_SPLIT_RE.split(text)
    for _start, _stop, keys in _key_runs(lines):
        for name in _matched_signatures(set(keys)):
            found.setdefault(f"{STRUCTURE_LEAK_PREFIX}{name}")
    return list(found)


def normalize_for_judge(text: str, numbering: SourceNumbering | None = None) -> str:
    """Rewrite provider-identifying shapes in ``text`` into neutral tokens.

    Structure is neutralized FIRST: a registered vendor frontmatter key run
    collapses to :data:`CANONICAL_FRONTMATTER_KEYS` in a fixed block, which is
    what makes the FRONTMATTER of a structure swap byte-identical (the body
    scaffolding is the named remainder — see the module docstring) and what
    removes ``exomem_id:`` as a key rather than scrubbing it into a
    ``[system]_id:`` residue. Then source-like shapes (refs, sentinels, vault paths) become
    ``[ctx:N]`` — a sentinel and a bare mention of the same source id share one
    N — and product names become :data:`NEUTRAL_SYSTEM_TOKEN`.
    """

    numbers = numbering if numbering is not None else SourceNumbering()

    def _ctx_whole(match: re.Match[str]) -> str:
        return numbers.token(match.group(0))

    def _ctx_sentinel(match: re.Match[str]) -> str:
        return numbers.token(match.group(1))

    out = _canonicalize_structure(text)
    out = _EXOMEM_URI_RE.sub(_ctx_whole, out)
    out = SENTINEL_RE.sub(_ctx_sentinel, out)
    out = _VAULT_PATH_RE.sub(_ctx_whole, out)
    out = _MD_PATH_RE.sub(_ctx_whole, out)
    out = _BARE_SOURCE_ID_RE.sub(_ctx_whole, out)  # after sentinels: ids share their N
    for pattern in _PRODUCT_RES:
        out = pattern.sub(NEUTRAL_SYSTEM_TOKEN, out)
    return out


def leakage_scan(text: str) -> list[str]:
    """Residual provider-identifying tokens AND structure in ``text``.

    Empty = clean. Used as a hard gate by the handshake writer: any hit means
    the request is refused, never written. Token matches are returned verbatim
    (deduplicated, order of first appearance) so refusals are explainable; a
    structural hit has no single verbatim token to quote and is named instead,
    as ``structure:<signature>``, appended after the token hits.
    """

    found: dict[str, None] = {}
    for _kind, pattern in _LEAK_RES:
        for match in pattern.finditer(text):
            found.setdefault(match.group(0))
    for marker in structural_leakage_scan(text):
        found.setdefault(marker)
    return list(found)
