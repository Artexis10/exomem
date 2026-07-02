"""Regression backstop: the shipped generic scaffold must not contain personal data.

`src/exomem/_scaffold/` is the generic starter skill shipped to new users (via
`init` / `install-skill`). Its `_Schema/` is a HAND-AUTHORED, deliberately-generic
starter (not derived from any private vault). This test scans the COMMITTED
scaffold two ways:

1. `test_scaffold_ships_no_personal_data` — structural machine-path/personal
   patterns (the `LEAK_PATTERNS` shared with the retired genericize script + the
   still-live rebuild-schema-zip tooling), so the structural guard never drifts.
2. `test_scaffold_ships_no_personal_tokens` — an explicit denylist of the
   synthetic private names, products, domains, and vault-structure labels. This
   is the hard wall: the leak class (shipping private tenant/product/collaborator
   names) cannot recur on a hand-edit.

Both run without the vault, so they work in CI.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import exomem

# The whole shipped scaffold (not just _Schema): index/log stubs, Sources/Notes/
# Entities indexes, and the _Schema docs all ship to new users.
SCAFFOLD = Path(exomem.__file__).resolve().parent / "_scaffold"
SCAFFOLD_SCHEMA = SCAFFOLD / "_Schema"
SOURCE = Path(exomem.__file__).resolve().parent  # src/exomem/
REPO = Path(exomem.__file__).resolve().parent.parent.parent  # src/exomem -> repo root


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


def _source_files() -> list[Path]:
    return [f for f in sorted(SOURCE.rglob("*")) if f.is_file()]


def _leak_patterns() -> list[str]:
    """Import the structural pattern list from the (hyphenated) genericize script
    by path, so the test and the generator never drift."""
    spec = importlib.util.spec_from_file_location(
        "genericize_schema", REPO / "scripts" / "genericize-schema.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.LEAK_PATTERNS


def _scaffold_files() -> list[Path]:
    return [f for f in sorted(SCAFFOLD.rglob("*")) if f.is_file()]


def test_scaffold_ships_no_personal_data() -> None:
    """Structural machine-path/personal patterns (shared with the generator)."""
    patterns = [re.compile(p) for p in _leak_patterns()]
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
