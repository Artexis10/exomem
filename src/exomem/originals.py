"""Import-light identity and presence of original evidence, without store I/O."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

CONFIG_ENV = "EXOMEM_MEDIA_STORE_CONFIG"


@dataclass(frozen=True, slots=True)
class StoreBinding:
    store: str


@dataclass(frozen=True, slots=True)
class BinaryBinding:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class StoredObject:
    schema: int
    store: str
    key_version: str
    object: str
    version: str
    ciphertext_size: int
    ciphertext_sha256: str

    def fields(self) -> dict[str, object]:
        from dataclasses import asdict

        return asdict(self)


@dataclass(frozen=True, slots=True)
class OriginalSnapshot:
    state: Literal["local", "cached", "evicted", "missing"]
    path: Path
    sidecar_path: Path
    binary_sha256: str | None = None
    binary_size: int | None = None
    reference: StoredObject | None = None
    sidecar_hash: str | None = None
    offloadable: bool = False

    @property
    def present(self) -> bool:
        """Logical presence: evicted evidence remains an original."""
        return self.state != "missing"


def is_sha256(value: object) -> bool:
    # SHA-256's lowercase hexadecimal format is fixed by this descriptor schema.
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def stored_reference(value: object) -> StoredObject | None:
    # These are the closed version-1 protocol fields, not a user vocabulary.
    if not isinstance(value, dict) or set(value) != {
        "schema", "store", "key_version", "object", "version",
        "ciphertext_size", "ciphertext_sha256",
    }:
        return None
    if type(value["schema"]) is not int or value["schema"] != 1:
        return None
    for field in ("store", "key_version", "version"):
        item = value[field]
        if not isinstance(item, str) or not item or len(item) > 1024 or any(ord(c) < 32 for c in item):
            return None
    if not is_sha256(value["object"]) or not is_sha256(value["ciphertext_sha256"]):
        return None
    if type(value["ciphertext_size"]) is not int or value["ciphertext_size"] <= 0:
        return None
    return StoredObject(**value)


def bound_path(fields: dict[str, object]) -> str | None:
    """The media, dataset and generic companion schemas name one original."""
    descriptor = fields.get("governance_companion")
    # These are closed pointer fields in the existing sidecar schemas.
    paths = [fields[key] for key in ("evidence_file", "data_file") if key in fields]
    if isinstance(descriptor, dict) and "artifact_path" in descriptor:
        paths.append(descriptor["artifact_path"])
    if not paths or any(not isinstance(path, str) or path != paths[0] for path in paths):
        return None
    return paths[0]


def is_note_path(relative: str) -> bool:
    """A vault note is never offloaded, so it is never evicted (design D1).

    `.md` is the note extension that the vault format fixes, not a vocabulary.
    """
    return relative.lower().endswith(".md")


def binary_binding(fields: dict[str, object]) -> BinaryBinding | None:
    path = bound_path(fields)
    digest = fields.get("binary_sha256")
    size = fields.get("binary_size")
    if path is None or not is_sha256(digest) or type(size) is not int or size < 0:
        return None
    return BinaryBinding(path, digest, size)


def reference_companion_fields(binary: BinaryBinding, reference: StoredObject) -> dict[str, object]:
    """The existing descriptor fields needed by a reference-only Trash companion."""
    return {"evidence_file": binary.path, "binary_sha256": binary.sha256,
            "binary_size": binary.size, "stored_object": reference.fields()}


def is_reference_only_companion(fields: dict[str, object], body: str) -> bool:
    """Custom page content is never disposable retention metadata."""
    binary = binary_binding(fields)
    reference = stored_reference(fields.get("stored_object"))
    return binary is not None and reference is not None and not body.strip() and fields == reference_companion_fields(binary, reference)


def referenced_binding(
    fields: dict[str, object], *, sidecar_path: str, store: StoreBinding,
) -> tuple[BinaryBinding, StoredObject] | None:
    """Read a canonical reference without requiring the original's physical presence.

    A caller supplies the companion's authoritative path before any Trash relocation.
    Missing or foreign references count as none; malformed selected-store custody raises.
    """
    from . import reserved_paths
    from .governance.raw_protection import marked
    from .kbdir import kb_dirname
    from .preserve import _sidecar_paths

    if "stored_object" not in fields:
        return None
    reference = stored_reference(fields["stored_object"])
    if reference is None:
        raise ValueError("stored reference is malformed")
    if reference.store != store.store:
        return None
    binary = binary_binding(fields)
    if binary is None or reserved_paths.classify_logical(binary.path).blocked:
        raise ValueError("stored reference has no valid binary binding")
    target = Path(binary.path)
    candidates = _sidecar_paths(
        target, protected=marked(binary.path),
        source=target.is_relative_to(Path(kb_dirname()) / "Sources"),
    )
    if sidecar_path not in {candidate.as_posix() for candidate in candidates}:
        raise ValueError("stored reference is not at its bound companion path")
    return binary, reference


def configuration(vault: Path) -> dict[str, object] | None:
    """Read explicit per-vault configuration, retaining identity while disabled.

    Configuration errors propagate: silently losing a selected store identity
    would turn every evicted original into an apparent deletion.
    """
    configured = os.environ.get(CONFIG_ENV)
    if not configured:
        return None
    value = json.loads(Path(configured).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("vaults"), dict):
        raise ValueError("media store configuration requires vaults")
    selected = value["vaults"].get(str(Path(vault).resolve()))
    if selected is None:
        return None
    if not isinstance(selected, dict) or not isinstance(selected.get("store"), str) or not selected["store"]:
        raise ValueError("media store configuration requires a stable store identity")
    return selected


def binding_for(vault: Path) -> StoreBinding | None:
    value = configuration(vault)
    return StoreBinding(str(value["store"])) if value is not None else None


def resolve_original_relative(vault: Path, value: str) -> str:
    """Keep physical spelling checks, admitting a missing leaf only by its binding."""
    from . import reserved_paths
    from .vault import canonical_vault_rel

    try:
        return reserved_paths.resolve_physical_relative(vault, value)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code != "MISSING":
            raise
        relative = canonical_vault_rel(vault, value)
        if original_state(vault, relative).state != "evicted":
            raise
        return relative


def sidecar_path(path: Path) -> Path:
    from .governance.raw_protection import marked
    from .preserve import _sidecar_path

    return _sidecar_path(path, protected=marked(path.as_posix()))


def evicted(vault: Path, path: str | Path) -> bool:
    """Whether a path names an evicted original; an unbound vault and a note never do."""
    if binding_for(vault) is None:
        return False
    return not is_note_path(Path(path).as_posix()) and original_state(vault, path).state == "evicted"


def bound_companion(vault: Path, relative: str) -> tuple[str, dict[str, object], bytes] | None:
    """Return the first companion candidate whose pointer fields all name this original.

    preserve owns the candidate layout, legacy Source forms included. A malformed
    candidate raises UnicodeError or FrontmatterError; each caller decides what it means.
    """
    from . import reserved_paths
    from .governance.raw_protection import marked
    from .kbdir import kb_dirname
    from .preserve import _sidecar_paths
    from .vault import parse_frontmatter

    target = Path(relative)
    for candidate in _sidecar_paths(
        target, protected=marked(relative), source=target.is_relative_to(Path(kb_dirname()) / "Sources"),
    ):
        companion = candidate.as_posix()
        try:
            data = reserved_paths.read_generic_bytes(vault, companion).data
        except reserved_paths.ReservedPathLeafError as error:
            if error.code != "MISSING":
                raise
            continue
        fields, _body, marker = parse_frontmatter(data.decode("utf-8"), custody=True)
        if marker is not None and bound_path(fields) == relative:
            return companion, fields, data
    return None


def original_state(vault: Path, path: str | Path, *, binding: StoreBinding | None = None) -> OriginalSnapshot:
    """Resolve local, cached, evicted or missing from one exact sibling binding.

    This reader never constructs a store client or opens the job database.
    Foreign references deliberately confer no presence in the selected vault.
    """
    from . import reserved_paths
    from .vault import FrontmatterError

    target = Path(path)
    if target.is_absolute():
        # A caller may spell the root through a symlink; both spellings name this vault.
        root = Path(vault)
        target = target.relative_to(root if target.is_relative_to(root) else root.resolve())
    vault = Path(vault).resolve()
    relative = target.as_posix()
    canonical = PurePosixPath(relative)
    if not relative or relative != canonical.as_posix() or any(p in {".", ".."} for p in canonical.parts):
        raise ValueError("original path must be canonical and vault-relative")
    binary = vault / target
    try:
        reserved_paths.inspect_generic_file(vault, relative)
        physical = True
    except reserved_paths.ReservedPathLeafError as error:
        if error.code != "MISSING":
            raise
        physical = False
    try:
        found = bound_companion(vault, relative)
    except (UnicodeError, FrontmatterError):
        found = None
    if found is None:
        from .governance.raw_protection import marked
        from .preserve import _sidecar_path

        return OriginalSnapshot("local" if physical else "missing", binary,
                                vault / _sidecar_path(target, protected=marked(relative)))
    companion, fields, data = found
    sidecar = vault / companion
    binary_binding_value = binary_binding(fields)
    if binary_binding_value is None:
        return OriginalSnapshot("local" if physical else "missing", binary, sidecar)
    reference = stored_reference(fields.get("stored_object"))
    selected = binding if binding is not None else binding_for(vault)
    if selected is None or (reference is not None and reference.store != selected.store):
        reference = None
    state = ("cached" if physical else "evicted") if reference is not None else ("local" if physical else "missing")
    return OriginalSnapshot(
        state, binary, sidecar, binary_binding_value.sha256, binary_binding_value.size,
        reference, hashlib.sha256(data).hexdigest(),
        # Datasets stay on the volume with notes (design D1); `data_file` is their closed pointer field.
        offloadable=not is_note_path(relative) and "data_file" not in fields,
    )
