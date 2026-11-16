"""Whether each custody pointer of a Source card still names its bytes.

A Source or Evidence card records where its raw artifact lives in the structured pointer fields
that `originals` names: the scalar pointers and the `artifact_path` inside a descriptor. A move
that does not know the card (an unpaired card carries a name unlike its file's) leaves such a
pointer naming a path that no longer exists. This module answers whether each pointer still
holds; `audit` reports the answer and `reclassify_source` repairs it.

A card with no structured pointer is read the other way round: the vault paths its body cites
in a markdown code span under a custody tree. The body of a Source never changes, so a stale
citation there can only be answered by giving the card a structured pointer.

Each answer is the one the caller may know. A file the caller may not see reads as absent, as
every write door answers it, and a path that custody refuses to read (a link, a reserved name,
a folder) is unreadable, never missing.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import markdown_regions, originals, reserved_paths
from .backfill import iter_kb_files
from .kbdir import managed_join
from .vault import APPEND_ONLY_KB_SUBPATHS, in_append_only_tree, kb_root

#: The card field that stands for a body citation in a finding.
BODY_FIELD = "body"

PRESENT = "present"
MISSING = "missing"
SIZE = "size"
OUTSIDE_VAULT = "outside_vault"
UNREADABLE = "unreadable"
#: One line per problem, for the finding that reports it.
PROBLEM_TEXT = {
    MISSING: "no file exists at that path",
    SIZE: "the file's size differs from the recorded size",
    OUTSIDE_VAULT: "the path does not stay inside the vault",
    UNREADABLE: "custody does not read a plain file there (a link, a reserved name or a folder)",
}


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


@dataclass(frozen=True)
class Located:
    """The bytes at one path as the caller may know them."""

    state: str
    size: int | None = None
    #: The SHA-256 the media store's binding records, for an original it holds instead of the vault.
    stored_sha256: str | None = None


def is_nested(field: str) -> bool:
    return "." in field


def pointers(frontmatter: Mapping[str, object]) -> list[Pointer]:
    """Every structured pointer the card records, in schema order."""
    found: list[Pointer] = []
    for name in originals.SCALAR_POINTERS:
        value = frontmatter.get(name)
        if isinstance(value, str) and value:
            found.append(Pointer(name, value))
    for name in originals.NESTED_POINTERS:
        block = frontmatter.get(name)
        value = block.get(originals.NESTED_PATH_KEY) if isinstance(block, Mapping) else None
        if isinstance(value, str) and value:
            found.append(Pointer(f"{name}.{originals.NESTED_PATH_KEY}", value))
    return found


def bound(card_rel: str, field: str, path: str) -> bool:
    """Whether the card follows its bytes by name, so the repair moves the file, not the pointer.

    A descriptor pointer follows the file's sibling name, and so does any pointer of a card
    that sits at its file's companion path.
    """
    if is_nested(field):
        return True
    return canonical_relative(path) is not None and card_rel in originals.companion_paths(path)


def body_citations(body: str) -> list[str]:
    """File paths under a custody tree that the body cites in a markdown code span.

    A span counts only when its last segment has an extension: a folder or an extensionless
    page name in a code span is a reference to a place, and the check cannot tell it from a
    missing file.
    """
    heads = tuple(managed_join(f"{tree}/") for tree in APPEND_ONLY_KB_SUBPATHS)
    cited: list[str] = []
    for span in markdown_regions.inline_code(body):
        candidate = span.strip()
        head, colon, line = candidate.rpartition(":")
        if colon and line.isascii() and line.isdigit():
            # `path:<line>` is the editor convention for a line of the file; the line is no part of the path.
            candidate = head
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


def withheld_filter(vault_root: Path) -> Callable[[str], bool] | None:
    """What this caller may not see, decided as `egress.write_target_withheld` decides it.

    None for the owner and on an ungoverned vault. The decision reads the path alone, so a
    withheld path reads the same whether or not a file is there.
    """
    from .governance import egress

    if not egress.caller_restricted(vault_root):
        return None
    keep = egress.restricted_release_filter(vault_root)
    return None if keep is None else (lambda rel: not keep(rel))


def locate(vault_root: Path, rel: str, withheld: Callable[[str], bool] | None) -> Located:
    """The state of the bytes at `rel`, with their size when they are present.

    Custody opens the path without reading a byte, and the media store's binding is read only
    for a path with no file.
    """
    if canonical_relative(rel) is None:
        return Located(OUTSIDE_VAULT)
    if reserved_paths.classify_logical(rel).blocked:
        return Located(UNREADABLE)
    if withheld is not None and withheld(rel):
        return Located(MISSING)
    try:
        # Custody refuses a link at any depth, a folder and a reserved name, as it does for a read.
        reserved_paths.inspect_generic_file(vault_root, rel)
        size = os.lstat(Path(vault_root) / rel).st_size
    except reserved_paths.ReservedPathLeafError as error:
        # MISSING is the custody code for an absent name; every other code is a refusal.
        return _stored(vault_root, rel) if error.code == "MISSING" else Located(UNREADABLE)
    except OSError:
        return Located(UNREADABLE)
    return Located(PRESENT, size)


def _stored(vault_root: Path, rel: str) -> Located:
    """An original the media store holds is present at the size its binding records."""
    # Outside the guard: a malformed store configuration must fail loudly, never read as absence.
    if originals.binding_for(vault_root) is None or originals.is_note_path(rel):
        return Located(MISSING)
    try:
        snapshot = originals.original_state(vault_root, rel)
    except reserved_paths.ReservedPathLeafError:
        return Located(UNREADABLE)
    if snapshot.state != "evicted":
        return Located(MISSING)
    return Located(PRESENT, snapshot.binary_size, snapshot.binary_sha256)


def recorded_size(frontmatter: Mapping[str, object], pointer: Pointer) -> int | None:
    """The byte size the card records for the bytes `pointer` names; a raw-protection block records none."""
    if not is_nested(pointer.field):
        value = frontmatter.get("binary_size")
    elif pointer.field.partition(".")[0] == originals.GOVERNANCE_COMPANION:
        block = frontmatter.get(originals.GOVERNANCE_COMPANION)
        value = block.get("artifact_size") if isinstance(block, Mapping) else None
    else:
        value = None
    return value if type(value) is int else None


def target_refusal(
    vault_root: Path, frontmatter: Mapping[str, object], target: str
) -> tuple[str, str] | None:
    """The `(code, reason)` that refuses `target` as the file a card's scalar pointer names, or None.

    A target is accepted only when it is a plain raw file under a custody tree that the caller
    may see and that fits the size and SHA-256 the card records. A file the caller may not see
    is refused exactly as a missing one, so no answer reveals it. This reads the file once,
    because it answers an explicit repair, not a sweep.
    """
    vault_root = Path(vault_root)
    if canonical_relative(target) is None:
        return "POINTER_INVALID", f"{target!r} is not a vault-relative path inside the vault."
    if in_append_only_tree(target) is None:
        trees = " or ".join(f"{tree}/" for tree in APPEND_ONLY_KB_SUBPATHS)
        return "POINTER_OUTSIDE_CUSTODY", f"{target} is not under {trees}."
    if originals.is_note_path(target):
        return "POINTER_INVALID", f"{target} is a page; a custody pointer names the raw file."
    found = locate(vault_root, target, withheld_filter(vault_root))
    if found.state == UNREADABLE:
        return "POINTER_INVALID", f"{target}: {PROBLEM_TEXT[UNREADABLE]}."
    if found.state != PRESENT:
        return "POINTER_TARGET_MISSING", f"no file exists at {target}."
    size = frontmatter.get("binary_size")
    if type(size) is int and found.size is not None and found.size != size:
        return "POINTER_MISMATCH", f"{target} is {found.size} bytes; the card records {size}."
    sha = frontmatter.get("binary_sha256")
    if not originals.is_sha256(sha):
        return None
    actual = found.stored_sha256 if found.stored_sha256 is not None else _sha256(vault_root, target)
    if actual is None:
        return "POINTER_INVALID", f"{target}: {PROBLEM_TEXT[UNREADABLE]}."
    if actual != sha:
        return "POINTER_MISMATCH", f"{target} does not hash to the SHA-256 the card records."
    return None


class Inspector:
    """Judges the pointers of many cards in one audit run, as the calling principal may know them.

    A pointer drifts when its path is missing, unreadable, leaves the vault, or names a file of
    another size than the card records. Content fixity is `original_fixity`'s, so nothing here
    reads a file's bytes.
    """

    def __init__(self, vault_root: Path) -> None:
        self._vault = Path(vault_root)
        self._withheld = withheld_filter(self._vault)
        self._names: dict[str, dict[str, list[str]]] = {}

    def inspect_card(self, frontmatter: Mapping[str, object], body: str) -> list[Drift]:
        """The drift of one card: its structured pointers, or its body citations when it has none.

        The structured pointer is the card's authority, so a card that has one never has its
        prose read: repairing a pointer must end the finding, and the body cannot be edited.
        """
        found = pointers(frontmatter) or [Pointer(BODY_FIELD, cited) for cited in body_citations(body)]
        drifts: list[Drift] = []
        for pointer in found:
            located = locate(self._vault, pointer.path, self._withheld)
            problem = located.state if located.state != PRESENT else None
            if problem is None and pointer.field != BODY_FIELD:
                size = recorded_size(frontmatter, pointer)
                if size is not None and located.size is not None and located.size != size:
                    problem = SIZE
            if problem is not None:
                drifts.append(Drift(pointer.field, pointer.path, problem))
        return drifts

    def candidate(self, recorded_path: str) -> str | None:
        """The one same-named file in the custody tree of `recorded_path` that the caller may see.

        Two same-named files are ambiguous, and an ambiguous name proposes nothing. A path outside
        the custody trees has no tree to search.
        """
        tree = in_append_only_tree(recorded_path)
        if tree is None:
            return None
        found = [
            rel
            for rel in self._name_index(tree).get(PurePosixPath(recorded_path).name, ())
            if rel != recorded_path and locate(self._vault, rel, self._withheld).state == PRESENT
        ]
        return found[0] if len(found) == 1 else None

    def _name_index(self, tree: str) -> dict[str, list[str]]:
        # Built once on first use: only a drifted pointer needs it, so a healthy vault never walks.
        if tree not in self._names:
            index: dict[str, list[str]] = {}
            for path in _custody_files(self._vault, tree):
                index.setdefault(path.name, []).append(path.relative_to(self._vault).as_posix())
            self._names[tree] = index
        return self._names[tree]


def _custody_files(vault_root: Path, tree: str) -> Iterator[Path]:
    root = kb_root(vault_root) / tree
    return iter_kb_files(root) if root.is_dir() else iter(())


def _sha256(vault_root: Path, rel: str) -> str | None:
    """The SHA-256 of the plain file at `rel`, read through custody; None when custody refuses it."""
    try:
        with reserved_paths.open_generic_binary(vault_root, rel) as source:
            return hashlib.file_digest(source.stream, "sha256").hexdigest()
    except reserved_paths.ReservedPathLeafError:
        return None
