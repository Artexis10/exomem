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


def binary_binding(fields: dict[str, object]) -> BinaryBinding | None:
    path = bound_path(fields)
    digest = fields.get("binary_sha256")
    size = fields.get("binary_size")
    if path is None or not is_sha256(digest) or type(size) is not int or size < 0:
        return None
    return BinaryBinding(path, digest, size)


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


def original_state(vault: Path, path: str | Path, *, binding: StoreBinding | None = None) -> OriginalSnapshot:
    """Resolve local, cached, evicted or missing from one exact sibling binding.

    This reader never constructs a store client or opens the job database.
    Foreign references deliberately confer no presence in the selected vault.
    """
    from . import reserved_paths
    from .governance.raw_protection import marked
    from .kbdir import kb_dirname
    from .preserve import _sidecar_paths
    from .vault import FrontmatterError, parse_frontmatter

    vault = Path(vault).resolve()
    target = Path(path)
    if target.is_absolute():
        target = target.relative_to(vault)
    relative = target.as_posix()
    canonical = PurePosixPath(relative)
    if not relative or relative != canonical.as_posix() or any(p in {".", ".."} for p in canonical.parts):
        raise ValueError("original path must be canonical and vault-relative")
    binary = vault / target
    # preserve owns the companion layout, legacy Source forms included; one rule decides both.
    candidates = tuple(vault / candidate for candidate in _sidecar_paths(
        target, protected=marked(relative), source=target.is_relative_to(Path(kb_dirname()) / "Sources"),
    ))
    try:
        reserved_paths.inspect_generic_file(vault, relative)
        physical = True
    except reserved_paths.ReservedPathLeafError as error:
        if error.code != "MISSING":
            raise
        physical = False
    base = OriginalSnapshot("local" if physical else "missing", binary, candidates[0])
    for sidecar in candidates:
        try:
            snapshot = reserved_paths.read_generic_bytes(vault, sidecar.relative_to(vault).as_posix())
            fields, _body, marker = parse_frontmatter(snapshot.data.decode("utf-8"), strict=True)
        except reserved_paths.ReservedPathLeafError as error:
            if error.code != "MISSING":
                raise
            continue
        except (UnicodeError, FrontmatterError):
            return base
        if marker is None or bound_path(fields) != relative:
            continue
        binary_binding_value = binary_binding(fields)
        if binary_binding_value is None:
            return base
        reference = stored_reference(fields.get("stored_object"))
        selected = binding if binding is not None else binding_for(vault)
        if selected is None or (reference is not None and reference.store != selected.store):
            reference = None
        return OriginalSnapshot(
            ("cached" if physical else "evicted") if reference is not None else base.state,
            binary, sidecar, binary_binding_value.sha256, binary_binding_value.size,
            reference, hashlib.sha256(snapshot.data).hexdigest(),
        )
    return base
