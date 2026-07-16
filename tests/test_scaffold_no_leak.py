"""Regression backstop: the shipped generic scaffold must not contain personal data.

`src/exomem/_scaffold/` is the generic starter skill shipped to new users (via
`init` / `install-skill`). Its `_Schema/` is a HAND-AUTHORED, deliberately-generic
starter (not derived from any private vault). This test scans the COMMITTED
scaffold two ways:

1. `test_scaffold_ships_no_personal_data` — structural machine-path/personal
   patterns (the in-file `LEAK_PATTERNS` list), so the structural guard never drifts.
2. `test_scaffold_ships_no_personal_tokens` — an explicit denylist of the
   synthetic private names, products, domains, and vault-structure labels. This
   is the hard wall: the leak class (shipping private tenant/product/collaborator
   names) cannot recur on a hand-edit.

Both run without the vault, so they work in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import exomem

# The whole shipped scaffold (not just _Schema): index/log stubs, Sources/Notes/
# Entities indexes, and the _Schema docs all ship to new users.
SCAFFOLD = Path(exomem.__file__).resolve().parent / "_scaffold"
SCAFFOLD_SCHEMA = SCAFFOLD / "_Schema"
SOURCE = Path(exomem.__file__).resolve().parent  # src/exomem/

# Structural machine-path / personal-data patterns. Formerly imported from the
# retired scripts/genericize-schema.py; now owned here (the sole consumer). Patterns,
# NOT sensitive literals, so this file itself stays clean.
LEAK_PATTERNS = [
    r"/mnt/[a-z]/",                                              # WSL drive mounts
    r"[A-Za-z]:\\Users\\(?!<)",                                  # Windows user paths (real, not <placeholder>)
    r"[A-Za-z]:\\[A-Za-z0-9 _-]+\\(?:Personal|Archive|Documents)",  # Windows abs paths
    r"/Users/(?!<)[^/\s<]+/",                                    # macOS home (real, not <placeholder>)
    r"/home/(?!<)[^/\s<]+/",                                     # Linux home (real, not <placeholder>)
    r"~/\.claude/hooks/",                                        # author's hook wiring
    r"\bQ_MNT_ALLOWLIST\b",                                      # author's allowlist env
]


# Explicit synthetic private-token denylist. Each entry is (label, regex, case_insensitive).
# Careful matching avoids false positives on legitimate generic prose:
#   - word boundaries on short names,
#   - product/employer names are matched CASE-SENSITIVELY so they don't collide
#     with common verbs or the
#     all-caps "YOLO" model,
#   - bare "Q" and "tu" are deliberately NOT denylisted (too generic).
_CI = True   # case-insensitive
_CS = False  # case-sensitive
_PERSONAL_TOKENS: list[tuple[str, str, bool]] = [
    ("PrivateName", r"\bPrivateName\b", _CI),
    ("private-handle", r"private-handle", _CI),
    ("Private Collaborator", r"private\s+collaborator", _CI),
    ("Private Product", r"\bPrivateProduct\b", _CS),
    ("Private Tenant", r"private\s+tenant", _CI),
    ("Private Domain", r"private-domain\.example", _CI),
    ("Private Vault Label", r"private\s+vault\s+label", _CI),
    ("Private Family Case", r"private\s+family\s+case", _CI),
]


# Source-tree denylist (src/exomem/**). Distinct from the scaffold list above:
# the shipped Python SOURCE legitimately uses the bare architecture noun
# "substrate" (the "pure-substrate" principle), so this denylists
# synthetic private domain labels rather than bare architectural terms. Product
# names that collide with common words are matched case-sensitively.
_SOURCE_PERSONAL_TOKENS: list[tuple[str, str, bool]] = [
    ("PrivateName", r"\bPrivateName\b", _CI),
    ("private-handle", r"private-handle", _CI),
    ("Private Collaborator", r"private\s+collaborator", _CI),
    ("Private Product", r"\bPrivateProduct\b", _CS),
    ("Private Tenant", r"private\s+tenant", _CI),
    ("Private Domain", r"private-domain\.example", _CI),
    ("Private Vault Label", r"private\s+vault\s+label", _CI),
]

# Runtime and generic scaffold language must describe Exomem on its own terms.
# Direct contender names belong only in maintainer benchmark scripts/docs.
_SOURCE_COMPETITOR_TOKENS: list[tuple[str, str, bool]] = [
    ("Basic Memory", r"\bbasic[-_ ]memory\b", _CI),
]


def _source_files() -> list[Path]:
    return [f for f in sorted(SOURCE.rglob("*")) if f.is_file()]


def _scaffold_files() -> list[Path]:
    return [f for f in sorted(SCAFFOLD.rglob("*")) if f.is_file()]


SAMPLE_VAULT = SOURCE / "_sample_vault"


def test_scaffold_ships_no_personal_data() -> None:
    """Structural machine-path/personal patterns (shared with the generator)."""
    patterns = [re.compile(p) for p in LEAK_PATTERNS]
    offenders: list[str] = []
    for f in sorted(SCAFFOLD_SCHEMA.rglob("*")):
        if not f.is_file():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for p in patterns:
                if p.search(line):
                    offenders.append(
                        f"{f.relative_to(SCAFFOLD_SCHEMA)}:{i}: /{p.pattern}/ -> {line.strip()[:80]}"
                    )
    assert not offenders, (
        "personal-data patterns found in the shipped scaffold — the scaffold is "
        "hand-authored; fix it directly and keep it generic:\n"
        + "\n".join(offenders)
    )


def test_scaffold_ships_no_personal_tokens() -> None:
    """Explicit denylist: the author's names/products/podcast/domain/structure
    must not appear ANYWHERE under src/exomem/_scaffold/."""
    compiled = [
        (label, re.compile(rx, re.IGNORECASE if ci else 0))
        for label, rx, ci in _PERSONAL_TOKENS
    ]
    offenders: list[str] = []
    for f in _scaffold_files():
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for label, p in compiled:
                if p.search(line):
                    offenders.append(
                        f"{f.relative_to(SCAFFOLD)}:{i}: token {label!r} -> {line.strip()[:80]}"
                    )
    assert not offenders, (
        "personal tokens found in the shipped scaffold — genericize before "
        "shipping (the scaffold is the public face of the skill):\n"
        + "\n".join(offenders)
    )


def test_sample_vault_ships_no_personal_data() -> None:
    """Structural machine-path/personal patterns (shared with the generator),
    extended to the packaged demo sample vault: src/exomem/_sample_vault/**
    ships inside the wheel for `exomem demo`, exactly like the scaffold does,
    so it needs the same structural guard (mirrors
    test_scaffold_ships_no_personal_data above)."""
    patterns = [re.compile(p) for p in LEAK_PATTERNS]
    offenders: list[str] = []
    for f in sorted(SAMPLE_VAULT.rglob("*")):
        if not f.is_file():
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue  # binary sidecars in the sample vault are not text leak surfaces
        for i, line in enumerate(lines, 1):
            for p in patterns:
                if p.search(line):
                    offenders.append(
                        f"{f.relative_to(SAMPLE_VAULT)}:{i}: /{p.pattern}/ -> {line.strip()[:80]}"
                    )
    assert not offenders, (
        "personal-data patterns found in the packaged demo sample vault — it "
        "ships inside the wheel; fix it directly and keep it generic:\n"
        + "\n".join(offenders)
    )


def test_source_ships_no_personal_tokens() -> None:
    """The shipped Python source (src/exomem/**) must not name the author,
    their collaborators, products, or vault-structure labels.

    This is the hard wall for the SOURCE CODE de-identification pass: comments,
    docstrings, fallback constants, and config defaults must stay generic so an
    open-source release can't leak the original tenant. It deliberately allows
    the bare noun "substrate" (the pure-substrate architecture term) and pins
    synthetic private domain labels instead.
    """
    compiled = [
        (label, re.compile(rx, re.IGNORECASE if ci else 0))
        for label, rx, ci in _SOURCE_PERSONAL_TOKENS
    ]
    assert compiled, "denylist must be non-empty (test would be vacuous)"
    files = _source_files()
    assert files, "no files found under src/exomem/ — wrong scan root?"
    offenders: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # compiled/binary artifacts (e.g. __pycache__) — skip
        for i, line in enumerate(text.splitlines(), 1):
            for label, p in compiled:
                if p.search(line):
                    offenders.append(
                        f"{f.relative_to(SOURCE)}:{i}: token {label!r} -> {line.strip()[:80]}"
                    )
    assert not offenders, (
        "personal tokens found in the shipped Python source — genericize "
        "before open-sourcing:\n" + "\n".join(offenders)
    )


def test_source_ships_no_competitor_tokens() -> None:
    """Contender names stay in maintainer comparison material, not runtime."""
    compiled = [
        (label, re.compile(rx, re.IGNORECASE if ci else 0))
        for label, rx, ci in _SOURCE_COMPETITOR_TOKENS
    ]
    offenders: list[str] = []
    for f in _source_files():
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for label, pattern in compiled:
                if pattern.search(line):
                    offenders.append(
                        f"{f.relative_to(SOURCE)}:{i}: token {label!r} -> {line.strip()[:80]}"
                    )
    assert not offenders, (
        "competitor tokens found in shipped Python/scaffold source; keep direct "
        "comparisons in maintainer benchmark scripts/docs:\n" + "\n".join(offenders)
    )
