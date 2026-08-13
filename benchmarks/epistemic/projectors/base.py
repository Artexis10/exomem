"""Projector contract and the declaration-evidence checks tests enforce.

The fairness rule this module exists to make mechanical: *every field mapping
cites competitor-authored evidence.* A projector that maps a field without
saying which line of the provider's own documentation justifies the mapping is
asserting a capability on the provider's behalf, and that is exactly how a
comparative benchmark becomes an opinion. So the evidence string is required by
the schema, and :func:`verify_declaration_evidence` additionally dereferences
it: a ``path:line`` citation must resolve to a real line of a real file in this
repository, and anything that is neither a citation nor a URL is rejected.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path

from ..snapshot import (
    EpistemicStateSnapshot,
    FieldDeclaration,
    ProjectorMeta,
    parse_evidence_citation,
)


class DeclarationEvidenceError(AssertionError):
    """A field declaration cites evidence that does not check out."""


class Projector(ABC):
    """Read-only projection of one provider's observable state.

    Implementations must be pure with respect to time: ``taken_at`` is supplied
    by the caller and nothing in a projector may read the clock, so a snapshot
    is reproducible from its inputs alone.
    """

    name: str = "projector"
    version: str = "0.0.0"
    author: str = "benchmark-harness"
    endpoints_used: tuple[str, ...] = ()

    @abstractmethod
    def project(self, *, phase: str, taken_at: str) -> EpistemicStateSnapshot:
        """Return the neutral snapshot for ``phase`` as observed at ``taken_at``."""

    def declarations(self) -> tuple[FieldDeclaration, ...]:
        """Every field this projector maps, with competitor-authored evidence."""

        return ()

    def meta(self) -> ProjectorMeta:
        """Published size and surface count for this projector."""

        return ProjectorMeta(
            name=self.name,
            version=self.version,
            author=self.author,
            endpoints_used=self.endpoints_used,
            loc=module_line_count(type(self)),
            loc_code=module_code_line_count(type(self)),
        )


def _module_lines(target: type | object) -> list[str]:
    source_file = inspect.getsourcefile(target if isinstance(target, type) else type(target))
    if source_file is None:
        return []
    return Path(source_file).read_text(encoding="utf-8").splitlines()


def module_line_count(target: type | object) -> int:
    """Raw line count of the module that defines ``target``."""

    return len(_module_lines(target))


def module_code_line_count(target: type | object) -> int:
    """Non-blank, non-comment line count — **docstrings included**.

    Published alongside :func:`module_line_count` because the raw count is easy
    to inflate with blank lines and ``#`` commentary, and the asymmetry finding
    the spec wants is about how much *interpretation* a projector performs.
    Docstrings count as code under this rule by deliberate choice: a docstring
    that explains a field mapping is part of that mapping's justification, not
    decoration. Excluding them would need an AST pass, which is not worth the
    dependency for a reportable size signal.
    """

    return sum(
        1
        for line in _module_lines(target)
        if line.strip() and not line.strip().startswith("#")
    )


def declaration_evidence_paths(
    declarations: Iterable[FieldDeclaration],
) -> tuple[tuple[str, str, int], ...]:
    """``(field, path, line)`` for every citation-shaped evidence string."""

    found: list[tuple[str, str, int]] = []
    for declaration in declarations:
        parsed = parse_evidence_citation(declaration.evidence)
        if parsed is not None:
            found.append((declaration.field, parsed[0], parsed[1]))
    return tuple(found)


def verify_declaration_evidence(
    declarations: Iterable[FieldDeclaration], *, repo_root: Path
) -> None:
    """Raise :class:`DeclarationEvidenceError` on any unsourced declaration."""

    problems: list[str] = []
    for declaration in declarations:
        evidence = declaration.evidence.strip()
        if not evidence:
            problems.append(f"{declaration.field}: empty evidence")
            continue
        parsed = parse_evidence_citation(evidence)
        if parsed is None:
            if evidence.startswith(("http://", "https://")):
                continue
            problems.append(
                f"{declaration.field}: evidence {evidence!r} is neither a path:line "
                "citation nor a URL"
            )
            continue
        rel_path, line = parsed
        target = repo_root / rel_path
        if not target.is_file():
            problems.append(f"{declaration.field}: cited file {rel_path} does not exist")
            continue
        total = len(target.read_text(encoding="utf-8").splitlines())
        if not 1 <= line <= total:
            problems.append(
                f"{declaration.field}: cited line {line} is outside {rel_path} "
                f"({total} lines)"
            )
    if problems:
        raise DeclarationEvidenceError(
            "field declarations without usable competitor-authored evidence:\n  "
            + "\n  ".join(problems)
        )
