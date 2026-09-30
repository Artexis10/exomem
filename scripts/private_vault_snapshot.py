#!/usr/bin/env python
"""Local-only private-vault snapshot builder.

For the context-activation benchmark's private real-vault instrument
(OpenSpec change ``add-context-activation-benchmark``, task 1.3). Never run
in CI and never committed: this copies markdown pages from a real vault into
a digest-pinned local snapshot, excluding any page whose body contains a
fixture turn verbatim (meta-note contamination -- ``design.md``'s
2026-09-16 reproduction found exactly this: an insight note quoting a
fixture turn ranked first for that turn). The destination is refused outright
if it resolves inside any git checkout, so the CI privacy gate is unaffected:
this script's output can never land inside a repository for it to scan.

Usage::

    python scripts/private_vault_snapshot.py --source /path/to/vault --dest /path/outside/any/repo

Excluded pages are recorded by path only in the written manifest -- their
content is never read back out of this tool, quoted, or logged.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class SnapshotError(ValueError):
    """Raised for an unsafe destination or an unreadable source."""


@dataclasses.dataclass(frozen=True)
class SnapshotManifest:
    source: str
    taken_at: str
    file_count: int
    excluded: tuple[str, ...]
    digest: str


def _is_bare_repository(path: Path) -> bool:
    """Whether ``path`` is itself a bare repository (N4).

    A bare repo has no ``.git`` subdirectory -- ``HEAD``, ``objects/`` and
    ``refs/`` sit directly in the repository directory itself -- so
    ``_is_repository_checkout``'s ``.git``-subdirectory check alone misses
    it entirely. Requires all three markers so an unrelated directory that
    merely happens to be named ``*.git`` or contain one stray file is not
    misclassified.
    """

    return (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir()


def _is_repository_checkout(path: Path) -> bool:
    """Whether ``path`` sits inside a git working tree (main checkout, a
    worktree, or a bare repository -- N4)."""

    for ancestor in (path, *path.parents):
        if (ancestor / ".git").exists() or _is_bare_repository(ancestor):
            return True
    return False


def refuse_unsafe_destination(dest: Path) -> Path:
    """Refuse a destination that resolves inside any git checkout, including
    a bare repository (N4)."""

    resolved = Path(dest).expanduser().resolve()
    if _is_repository_checkout(resolved):
        raise SnapshotError(
            f"destination {resolved} sits inside a git checkout; the private vault "
            "snapshot must never be written inside any repository"
        )
    return resolved


def _normalize_for_contamination_check(text: str) -> str:
    """Casefold, collapse whitespace (incl. newlines), strip punctuation,
    and map common typographic variants to ASCII (N3): a line-wrapped quote
    or an em-dash/curly-quote rendering of a fixture turn is exactly as much
    contamination as a byte-identical one, and must not hide behind
    formatting the way a plain substring check would miss.
    """

    normalized = unicodedata.normalize("NFKD", text)
    normalized = normalized.translate(
        str.maketrans({"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"'})
    )
    normalized = normalized.casefold()
    normalized = re.sub(r"[^\w\s]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _contains_any(text: str, needles: Iterable[str]) -> str | None:
    normalized_text = _normalize_for_contamination_check(text)
    for needle in needles:
        if not needle:
            continue
        if needle in text:
            return needle
        if _normalize_for_contamination_check(needle) in normalized_text:
            return needle
    return None


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.md")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_snapshot(
    source: Path,
    dest: Path,
    *,
    fixture_turns: Iterable[str],
    taken_at: str,
) -> SnapshotManifest:
    """Copy every ``*.md`` page from ``source`` into ``dest``.

    A page whose body contains any ``fixture_turns`` entry verbatim is
    excluded and listed (by path only) in the returned manifest, which is
    also written to ``dest / "SNAPSHOT_MANIFEST.json"``. ``dest`` is refused
    (:class:`SnapshotError`) if it resolves inside any git checkout. A page
    that cannot be decoded as UTF-8 text is excluded rather than guessed at.
    """

    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise SnapshotError(f"source vault {source} is not a directory")
    dest = refuse_unsafe_destination(dest)
    dest.mkdir(parents=True, exist_ok=True)

    turns = tuple(t for t in fixture_turns if t)
    excluded: list[str] = []
    copied = 0
    for path in sorted(source.rglob("*.md")):
        rel = path.relative_to(source).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            excluded.append(rel)
            continue
        if _contains_any(text, turns) is not None:
            excluded.append(rel)
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        copied += 1

    manifest = SnapshotManifest(
        source=str(source),
        taken_at=taken_at,
        file_count=copied,
        excluded=tuple(sorted(excluded)),
        digest=_digest_tree(dest),
    )
    (dest / "SNAPSHOT_MANIFEST.json").write_text(
        json.dumps(dataclasses.asdict(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_fixture_turns() -> tuple[str, ...]:
    """The committed fixture set's turns, imported lazily.

    Only the CLI entry point needs this: unit tests exercise
    :func:`build_snapshot` directly with an explicit ``fixture_turns``
    sequence, so importing the benchmark corpora package is never required
    just to load this module.
    """

    import sys

    benchmarks_dir = REPO_ROOT / "benchmarks"
    if str(benchmarks_dir) not in sys.path:
        sys.path.insert(0, str(benchmarks_dir))
    from epistemic.corpora.context_activation import ALL_TURNS

    return ALL_TURNS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Real vault root to snapshot (read-only).")
    parser.add_argument(
        "--dest", type=Path, required=True, help="Snapshot destination; refused if it resolves inside a repository."
    )
    parser.add_argument(
        "--fixture-turns-from",
        type=Path,
        default=None,
        help="Path to a JSON file holding a list of fixture turn strings to exclude on match "
        "(defaults to the committed context-activation fixture set's turns).",
    )
    args = parser.parse_args(argv)

    fixture_turns = (
        tuple(json.loads(args.fixture_turns_from.read_text(encoding="utf-8")))
        if args.fixture_turns_from is not None
        else _default_fixture_turns()
    )

    manifest = build_snapshot(args.source, args.dest, fixture_turns=fixture_turns, taken_at=_now())
    print(json.dumps(dataclasses.asdict(manifest), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
