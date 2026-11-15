"""The custody pointers of a Source card, and whether each still names its bytes.

A Source or Evidence card records where its raw artifact lives in structured fields: the
scalar pointers `evidence_file` and `data_file`, and the `artifact_path` inside the
`raw_protection` and `governance_companion` descriptors. A move that does not know the card
(an unpaired card carries a name unlike its file's) leaves such a pointer naming a path that
no longer exists. This module is the one place that names those fields and answers whether
they still hold; `audit` reports the answer and `reclassify_source` repairs it.

A card with no structured pointer is read the other way round: the vault paths its body cites
in a markdown code span under a custody tree. The body of a Source never changes, so a stale
citation there can only be answered by giving the card a structured pointer.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import originals
from .backfill import iter_kb_files
from .kbdir import kb_prefix
from .vault import APPEND_ONLY_KB_SUBPATHS, in_append_only_tree, kb_root

# These are the closed field names of the Source/Evidence card schema, not vault vocabulary.
SCALAR_POINTERS: tuple[str, ...] = ("evidence_file", "data_file")
NESTED_POINTERS: tuple[str, ...] = ("raw_protection", "governance_companion")
NESTED_PATH_KEY = "artifact_path"
#: The card field that stands for a body citation in a finding.
BODY_FIELD = "body"

# The recorded identity of the bytes each pointer names: (sha256, size, mtime_ns) field names.
# A nested descriptor records no mtime, and a raw-protection block records no size.
_SCALAR_IDENTITY = ("binary_sha256", "binary_size", "binary_mtime_ns")
_NESTED_IDENTITY: dict[str, tuple[str, str | None, str | None]] = {
    "governance_companion": ("artifact_sha256", "artifact_size", None),
    "raw_protection": ("artifact_sha256", None, None),
}

MISSING = "missing"
SIZE = "size"
CONTENT = "content"
OUTSIDE_VAULT = "outside_vault"
#: One line per problem, for the finding that reports it.
PROBLEM_TEXT = {
    MISSING: "no file exists at that path",
    SIZE: "the file's size differs from the recorded size",
    CONTENT: "the file's SHA-256 differs from the recorded one",
    OUTSIDE_VAULT: "the path does not stay inside the vault",
}

#: Bytes one audit run may hash, so a vault of large originals never makes the check unbounded.
#: Hashing starts only when a stat signature changed; a run that reaches the cap says so.
HASH_BYTE_BUDGET = 512 * 1024 * 1024
_CHUNK = 1024 * 1024

# A markdown inline code span: the syntax that delimits a cited path whole, spaces included.
_CODE_SPAN = re.compile(r"`([^`\n]+)`")


@dataclass(frozen=True)
class Pointer:
    #: The card field, dotted for a nested descriptor (`governance_companion.artifact_path`).
    field: str
    path: str


@dataclass(frozen=True)
class Drift:
    field: str
    recorded_path: str
    problem: str


def is_nested(field: str) -> bool:
    return "." in field


def pointers(frontmatter: Mapping[str, object]) -> list[Pointer]:
    """Every structured pointer the card records, in schema order."""
    found: list[Pointer] = []
    for name in SCALAR_POINTERS:
        value = frontmatter.get(name)
        if isinstance(value, str) and value:
            found.append(Pointer(name, value))
    for name in NESTED_POINTERS:
        block = frontmatter.get(name)
        value = block.get(NESTED_PATH_KEY) if isinstance(block, Mapping) else None
        if isinstance(value, str) and value:
            found.append(Pointer(f"{name}.{NESTED_PATH_KEY}", value))
    return found


def body_citations(body: str) -> list[str]:
    """File paths under a custody tree that the body cites in a markdown code span.

    A span counts only when its last segment has an extension: a folder or an extensionless
    page name in a code span is a reference to a place, and the check cannot tell it from a
    missing file.
    """
    prefix = kb_prefix()
    heads = tuple(f"{prefix}{tree}/" for tree in APPEND_ONLY_KB_SUBPATHS)
    cited: list[str] = []
    for span in _CODE_SPAN.findall(body):
        candidate = span.strip()
        if (
            candidate.startswith(heads)
            and PurePosixPath(candidate).suffix
            and not candidate.endswith("/")
            and candidate not in cited
        ):
            cited.append(candidate)
    return cited


def canonical_relative(path: str) -> str | None:
    """The vault-relative POSIX form of `path`, or None when it is not one that stays inside."""
    posix = PurePosixPath(path)
    if not path or posix.as_posix() != path or posix.is_absolute() or ".." in posix.parts:
        return None
    return path


def exists(vault_root: Path, rel: str) -> bool:
    """Whether the bytes at `rel` are in the vault: a file here, or an original stored elsewhere."""
    if canonical_relative(rel) is None:
        return False
    if (vault_root / rel).is_file():
        return True
    return originals.evicted(vault_root, rel)


def recorded_identity(
    frontmatter: Mapping[str, object], pointer: Pointer
) -> tuple[str | None, int | None, int | None]:
    """The (sha256, size, mtime_ns) the card records for the bytes `pointer` names."""
    if is_nested(pointer.field):
        block_name = pointer.field.partition(".")[0]
        sha_key, size_key, mtime_key = _NESTED_IDENTITY[block_name]
        source = frontmatter.get(block_name)
        if not isinstance(source, Mapping):
            return None, None, None
    else:
        sha_key, size_key, mtime_key = _SCALAR_IDENTITY
        source = frontmatter

    def number(key: str | None) -> int | None:
        value = source.get(key) if key else None
        return value if type(value) is int else None

    sha = source.get(sha_key)
    valid = sha if isinstance(sha, str) and originals.is_sha256(sha) else None
    return valid, number(size_key), number(mtime_key)


def target_refusal(
    vault_root: Path, frontmatter: Mapping[str, object], target: str
) -> tuple[str, str] | None:
    """The `(code, reason)` that refuses `target` as the file a card's pointer names, or None.

    A target is accepted only when it is a raw file under a custody tree that exists and fits
    the identity the card records for its bytes. This reads the file once, because it answers
    an explicit repair, not a sweep.
    """
    if canonical_relative(target) is None:
        return "POINTER_INVALID", f"{target!r} is not a vault-relative path inside the vault."
    if in_append_only_tree(target) is None:
        trees = " or ".join(f"{tree}/" for tree in APPEND_ONLY_KB_SUBPATHS)
        return "POINTER_OUTSIDE_CUSTODY", f"{target} is not under {trees}."
    if originals.is_note_path(target):
        return "POINTER_INVALID", f"{target} is a page; a custody pointer names the raw file."
    if not exists(Path(vault_root), target):
        return "POINTER_TARGET_MISSING", f"no file exists at {target}."
    sha, size, _ = recorded_identity(frontmatter, Pointer(SCALAR_POINTERS[0], target))
    path = Path(vault_root) / target
    if path.is_file():
        stored_size = path.stat().st_size
    else:
        stored_size = originals.original_state(Path(vault_root), target).binary_size
    if size is not None and stored_size is not None and stored_size != size:
        return "POINTER_MISMATCH", f"{target} is {stored_size} bytes; the card records {size}."
    if sha is not None and path.is_file() and _sha256(path) != sha:
        return "POINTER_MISMATCH", f"{target} does not hash to the SHA-256 the card records."
    return None


class Inspector:
    """Judges the pointers of many cards in one run with a shared hashing budget.

    A pointer drifts when its path is missing, leaves the vault, or its recorded size or
    SHA-256 no longer fits the bytes. The SHA-256 is read only when the recorded mtime no
    longer matches the file's, and only while the run's byte budget lasts; `unchecked`
    counts the pointers whose content stayed unread for that reason.
    """

    def __init__(self, vault_root: Path, *, hash_budget: int = HASH_BYTE_BUDGET) -> None:
        self._vault = vault_root
        self._budget = hash_budget
        self._names: dict[str, dict[str, list[str]]] = {}
        self.unchecked = 0

    def inspect_card(self, frontmatter: Mapping[str, object], body: str) -> list[Drift]:
        """The drift of one card: its structured pointers, or its body citations when it has none.

        The structured pointer is the card's authority, so a card that has one never has its
        prose read: repairing a pointer must end the finding, and the body cannot be edited.
        """
        found = pointers(frontmatter)
        if not found:
            return [
                Drift(BODY_FIELD, cited, MISSING)
                for cited in body_citations(body)
                if not exists(self._vault, cited)
            ]
        drifts: list[Drift] = []
        for pointer in found:
            problem = self._problem(frontmatter, pointer)
            if problem is not None:
                drifts.append(Drift(pointer.field, pointer.path, problem))
        return drifts

    def candidate(self, recorded_path: str) -> str | None:
        """The one same-named file in the custody tree of `recorded_path`, or None.

        Two same-named files are ambiguous, and an ambiguous name proposes nothing.
        """
        tree = in_append_only_tree(recorded_path)
        trees = (tree,) if tree is not None else APPEND_ONLY_KB_SUBPATHS
        found = {
            rel
            for name in trees
            for rel in self._name_index(name).get(PurePosixPath(recorded_path).name, [])
        }
        found.discard(recorded_path)
        return next(iter(found)) if len(found) == 1 else None

    def _name_index(self, tree: str) -> dict[str, list[str]]:
        # Built once on first use: only a drifted pointer needs it, so a healthy vault never walks.
        if tree not in self._names:
            index: dict[str, list[str]] = {}
            for path in _custody_files(self._vault, tree):
                index.setdefault(path.name, []).append(path.relative_to(self._vault).as_posix())
            self._names[tree] = index
        return self._names[tree]

    def _problem(self, frontmatter: Mapping[str, object], pointer: Pointer) -> str | None:
        if canonical_relative(pointer.path) is None:
            return OUTSIDE_VAULT
        sha, size, mtime_ns = recorded_identity(frontmatter, pointer)
        path = self._vault / pointer.path
        try:
            stat = path.stat() if path.is_file() else None
        except OSError:
            stat = None
        if stat is None:
            if not originals.evicted(self._vault, pointer.path):
                return MISSING
            # An evicted original keeps its size in the store; the fixity pass reads its bytes.
            stored = originals.original_state(self._vault, pointer.path).binary_size
            return SIZE if size is not None and stored is not None and stored != size else None
        if size is not None and stat.st_size != size:
            return SIZE
        if sha is None or mtime_ns is None or mtime_ns == stat.st_mtime_ns:
            return None
        if stat.st_size > self._budget:
            self.unchecked += 1
            return None
        self._budget -= stat.st_size
        return None if _sha256(path) == sha else CONTENT


def _custody_files(vault_root: Path, tree: str) -> Iterator[Path]:
    root = kb_root(vault_root) / tree
    return iter_kb_files(root) if root.is_dir() else iter(())


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            while chunk := stream.read(_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()

