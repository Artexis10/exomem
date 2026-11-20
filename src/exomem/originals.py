"""Import-light identity and presence of original evidence, without store I/O."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .kbdir import managed_join

CONFIG_ENV = "EXOMEM_MEDIA_STORE_CONFIG"

# The custody pointer fields of the Source and Evidence card schemas, named once here because
# this module is import-light: `source_pointers` and `move_file` read them from here.
SCALAR_POINTERS: tuple[str, ...] = ("evidence_file", "data_file")  # nosemgrep: ep-word-set -- closed card-schema field names, not vocabulary.
GOVERNANCE_COMPANION = "governance_companion"
RAW_PROTECTION = "raw_protection"
NESTED_POINTERS: tuple[str, ...] = (RAW_PROTECTION, GOVERNANCE_COMPANION)
#: The key inside a descriptor that names the original.
NESTED_PATH_KEY = "artifact_path"


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
    #: Physical pool storage, when the ordinary logical binary path has been retired.
    local_path: Path | None = None
    #: The replicas that the sidecar lists beside the eviction store's `reference`, earliest first. They never
    #: change `state`: only the `reference` in the selected store decides local, cached or evicted.
    replicas: tuple[StoredObject, ...] = ()

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


#: The sibling frontmatter key that lists the replicas beyond `stored_object`, the pointer of the store that serves
#: eviction. An older release ignores it, so a downgrade reads the pointer alone.
REPLICAS = "replicas"


def replica_references(value: object) -> tuple[StoredObject, ...]:
    """The well-formed entries of a sidecar's `replicas` list, in written order. Any other value lists none.

    An entry that does not parse is unknown, never a replica: this read skips it, and a writer keeps it as written.
    """
    if not isinstance(value, list):
        return ()
    return tuple(found for entry in value if (found := stored_reference(entry)) is not None)


def bound_path(fields: dict[str, object]) -> str | None:
    """The media, dataset and generic companion schemas name one original."""
    descriptor = fields.get(GOVERNANCE_COMPANION)
    paths = [fields[key] for key in SCALAR_POINTERS if key in fields]
    if isinstance(descriptor, dict) and NESTED_PATH_KEY in descriptor:
        paths.append(descriptor[NESTED_PATH_KEY])
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
    descriptor = fields.get("governance_companion")
    if isinstance(descriptor, dict):
        # Existing companion schemas carry either legacy binary fields or the bound artifact fields.
        if "binary_sha256" not in fields:
            digest = descriptor.get("artifact_sha256")
        elif "artifact_sha256" in descriptor and descriptor["artifact_sha256"] != digest:
            return None
        if "binary_size" not in fields:
            size = descriptor.get("artifact_size")
        elif "artifact_size" in descriptor and descriptor["artifact_size"] != size:
            return None
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


class RetirementUnconfirmed(ValueError):
    """A companion whose Trash retirement witness names it, but no longer confirms it.

    Its original is neither live nor absent, so each read refuses (`trash_metadata.retired_companion`). A tool
    refuses with `code` and `reason`, which name what the owner changes.
    """

    code = "ORIGINAL_RETIREMENT_UNCONFIRMED"

    def __init__(self, companion: str) -> None:
        self.companion = companion
        self.reason = (f"the Trash retirement of the original that {companion} names could not be confirmed; "
                       "empty or restore that Trash entry first")
        super().__init__(self.reason)


class DisplacedReference(ValueError):
    """A well-formed reference in a page that is not at its original's companion path.

    A page that a confined move carries alone, away from an evicted original that its caller may
    not see, keeps the original's binding. The page alone cannot show that the binding is its own,
    so it raises this; the census confirms it from the media ledger (`media_store.recorded_versions`).
    """

    def __init__(self, binary: BinaryBinding, reference: StoredObject) -> None:
        super().__init__("stored reference is not at its bound companion path")
        self.binary = binary
        self.reference = reference


def companion_binding(fields: dict[str, object], *, sidecar_path: str) -> BinaryBinding | None:
    """Validate the binding at its permitted ordinary, protected or legacy Source companion.

    A well-formed reference away from those paths raises `DisplacedReference`, which a census confirms
    from the media ledger; any other binding away from them raises a plain `ValueError`.
    """
    from . import reserved_paths

    binary = binary_binding(fields)
    if binary is None:
        return None
    if reserved_paths.classify_logical(binary.path).blocked:
        raise ValueError("original has an unsafe binary binding")
    if sidecar_path not in companion_paths(binary.path):
        reference = stored_reference(fields.get("stored_object"))
        if reference is not None:
            raise DisplacedReference(binary, reference)
        raise ValueError("original is not at its bound companion path")
    return binary


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


# The retrieval classes of a store's capability descriptor: a protocol value that the descriptor schema fixes, never
# a vocabulary that a vault defines. Only `immediate` lets repair read a replica without the owner.
IMMEDIATE = "immediate"
# nosemgrep: ep-word-set -- the descriptor schema's closed enum of retrieval classes.
RETRIEVAL_CLASSES: tuple[str, ...] = (IMMEDIATE, "minutes", "hours")
#: The configuration key that lists the declared stores beside the kept `store` key.
STORES = "stores"
#: The optional configuration key that states who holds the decryption identity, and its one value: a platform that
#: wraps it. Without the key, the owner's confirmed export of the identity is the only evidence of custody.
KEY_CUSTODY = "key_custody"
PLATFORM_CUSTODY = "platform"


@dataclass(frozen=True, slots=True)
class StoreEntry:
    """One configured store and its capability descriptor; a None field is unknown, never refused.

    `connection` holds the fields that its adapter reads. An entry whose descriptor omits a field (`missing`)
    or gives it a value that the schema refuses (`invalid`) is unavailable: no replica is made in it and repair
    does not read it, and the status names those fields. Nothing branches on `name`.
    """

    name: str
    connection: dict[str, object]
    failure_domain: str | None = None
    retrieval_class: str | None = None
    head_recomputes: bool | None = None
    missing: tuple[str, ...] = ()
    invalid: tuple[str, ...] = ()
    #: Whether this is the store that serves eviction: the one that the per-vault `store` key names.
    serves_eviction: bool = False
    #: Whether the entry came from the `stores` list (a declared store) and not from the legacy top-level fields.
    declared: bool = False

    @property
    def available(self) -> bool:
        return not self.missing and not self.invalid


def _descriptor(entry: dict[str, object], *, require_adapter: bool) -> tuple[dict[str, object], tuple[str, ...], tuple[str, ...]]:
    """The descriptor values that an entry gives, with the fields it omits and the fields it gives wrongly."""
    given: dict[str, object] = {}
    missing: list[str] = []
    invalid: list[str] = []
    checks = (
        ("failure_domain", lambda v: isinstance(v, str) and bool(v.strip()) and len(v) <= 1024),
        ("retrieval_class", lambda v: isinstance(v, str) and v in RETRIEVAL_CLASSES),
        ("head_recomputes", lambda v: type(v) is bool),
    )
    for field, valid in checks:
        if field not in entry:
            missing.append(field)
        elif valid(entry[field]):
            given[field] = entry[field]
        else:
            invalid.append(field)
    if require_adapter:
        if "adapter" not in entry:
            missing.append("adapter")
        elif not isinstance(entry["adapter"], str) or not entry["adapter"]:
            invalid.append("adapter")
    return given, tuple(missing), tuple(invalid)


def store_entries(config: dict[str, object]) -> dict[str, StoreEntry]:
    """Every store that a vault's configuration declares, the one that serves eviction first.

    The kept `store` key names the store that serves eviction, and the top-level fields of the entry (`adapter`,
    `root` and the rest) are its connection, as they were before declared stores: an older release reads the file
    the same way, so the two can never disagree. With no `stores` list, that is the only store, `immediate`, with
    its other descriptor fields unknown. A declared entry of that name supplies only its descriptor. Any other
    entry names a further store with its own connection. A malformed entry makes only itself unavailable.
    """
    primary = str(config["store"])
    declared = config.get(STORES)
    listed: list[tuple[str, dict[str, object]]] = []
    if isinstance(declared, list):
        for index, item in enumerate(declared):
            name = item.get("name") if isinstance(item, dict) else None
            listed.append((name if isinstance(name, str) and name else f"#{index}", item if isinstance(item, dict) else {}))
    found: dict[str, StoreEntry] = {}
    selected = next((entry for name, entry in listed if name == primary), None)
    if selected is None:
        found[primary] = StoreEntry(primary, config, retrieval_class=IMMEDIATE, serves_eviction=True)
    else:
        given, missing, invalid = _descriptor(selected, require_adapter=False)
        found[primary] = StoreEntry(primary, config, given.get("failure_domain"), given.get("retrieval_class"),
                                    given.get("head_recomputes"), missing, invalid, True, True)
    seen: set[str] = set()
    for name, item in listed:
        if name in seen:
            # A repeated name cannot be addressed, so it makes the later entry unavailable, not the first.
            found[f"{name}#repeat"] = StoreEntry(f"{name}#repeat", item, invalid=("name",), declared=True)
            continue
        seen.add(name)
        if name == primary:
            continue
        given, missing, invalid = _descriptor(item, require_adapter=True)
        if "name" not in item:
            missing = ("name", *missing)
        found[name] = StoreEntry(name, item, given.get("failure_domain"), given.get("retrieval_class"),
                                 given.get("head_recomputes"), missing, invalid, False, True)
    return found


def with_replica(value: object, replica: StoredObject) -> list[object]:
    """A sidecar's `replicas` list with `replica` in place of the entry for its store, or after the last entry.

    Every other entry stays as written and in its place, one that does not parse included, so a writer never drops
    what it did not change and the earliest entry stays first.
    """
    entries = list(value) if isinstance(value, list) else []
    for index, entry in enumerate(entries):
        held = stored_reference(entry)
        if held is not None and held.store == replica.store:
            entries[index] = replica.fields()
            return entries
    return [*entries, replica.fields()]


def store_entry(config: dict[str, object], name: str | None = None) -> StoreEntry | None:
    """The entry for `name`, or for the store that serves eviction when `name` is None; None when none is declared."""
    return store_entries(config).get(str(config["store"]) if name is None else name)


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
        snapshot = original_state(vault, relative)
        if snapshot.state != "evicted" and snapshot.local_path is None:
            raise
        return relative


def resolve_original_path(vault: Path, value: str) -> tuple[Path, str]:
    """Resolve an ordinary path or a canonically bound original whose bytes live elsewhere."""
    from . import reserved_paths
    from .vault import VaultPathError, resolve_under_vault

    try:
        return resolve_under_vault(vault, value, must_exist=True, must_be_file=True)
    except VaultPathError as missing:
        if missing.code != "NOT_FOUND":
            raise
        try:
            relative = resolve_original_relative(vault, value)
            return resolve_under_vault(vault, relative, must_be_file=True)
        except (ValueError, VaultPathError, reserved_paths.ReservedPathLeafError):
            raise missing from None


def sidecar_path(path: Path) -> Path:
    from .governance.raw_protection import marked
    from .preserve import _sidecar_path

    return _sidecar_path(path, protected=marked(path.as_posix()))


def available(vault: Path, path: str | Path) -> bool:
    """Whether the archive holds this original's bytes: an ordinary file, a shared pool object or a stored version.

    A sweep that asks whether an original is gone asks this, never whether its logical path is a file:
    a pooled filing has no file there.
    """
    target = Path(path) if Path(path).is_absolute() else Path(vault) / path
    if target.is_file():
        return True
    # nosemgrep: ep-word-membership -- the closed `OriginalState.state` literal; every state but "missing" holds bytes.
    return original_state(vault, target).state in {"local", "cached", "evicted"}


def may_pool(bound: str) -> bool:
    """Whether a binding whose companion names ``bound`` may keep its bytes as a shared pool object.

    Only a filing's bytes are shared. A Trash entry is no filing: its companion names its own Trash path,
    while a trashed filing's page keeps naming its original path. So a binding that names a Trash path is
    an entry, which keeps an ordinary file there even in a pooled archive.
    """
    from .trash_metadata import is_trash_path

    return not is_trash_path(bound)


def may_share_bytes(relative: str) -> bool:
    """Whether an original at `relative` may keep its bytes only in the shared pool.

    A pool-only filing exists on disk solely as its companion page `<name>.md`, which the egress
    artifact census tells from a note by `<name>` carrying a suffix of its own. A name without a
    suffix (`LICENSE`, or a dotfile like `.blob`) gives a companion indistinguishable from a note,
    so such an original keeps an ordinary file at its logical path instead, like a dataset.
    """
    # A trailing dot reads as a suffix of "." on newer Python and as none on older, so both count as none.
    return PurePosixPath(relative).suffix not in ("", ".")


def pooled(vault: Path, path: str | Path) -> OriginalSnapshot | None:
    """The filing of an original whose bytes are a shared pool object, which has no file at its logical path.

    `local_path` is that immutable object: read its identity or stat from it. A decoder that needs the
    filename takes `media_store.logical_view`. A damaged object is still pooled, and each read of it raises
    `original_pool.ObjectMismatch` or hashes to other bytes. Any other path, a missing or unsafe one included,
    is not pooled.
    """
    from . import original_pool

    # Pool objects exist only once the pool's marker is published, so an ordinary archive answers from the marker
    # alone and never opens its identity catalogue for this question.
    if not original_pool.enabled(vault):
        return None
    shared = digest_addressed(vault, path)
    return shared if shared is not None and shared.local_path is not None else None


def digest_addressed(vault: Path, path: str | Path, *, snapshot: OriginalSnapshot | None = None) -> OriginalSnapshot | None:
    """The filing, in a pooled archive, of an original that no ordinary file holds at its logical path.

    Its bytes are addressed by digest: a shared pool object, or a stored version once evicted. Moving or
    trashing it moves only its companion and repoints the binding; nothing renames or downloads bytes.
    A caller that already resolved the path passes its `snapshot`. Any other path, a missing or unsafe
    one included, is None.
    """
    from . import original_pool, reserved_paths

    target = Path(path) if Path(path).is_absolute() else Path(vault) / path
    if is_note_path(target.as_posix()):
        return None
    if snapshot is None:
        if target.is_file():
            return None
        try:
            snapshot = original_state(vault, target)
        except reserved_paths.ReservedPathLeafError:
            return None
        except RetirementUnconfirmed:
            raise  # Neither a filing nor absent: the caller refuses.
        except ValueError:
            original_pool.enabled(vault)  # An unreadable format marker raises: the layout is unknown, not ordinary.
            return None
    # `local_path` is set only when no ordinary file exists and the pool holds the object; an evicted original has
    # no local bytes at all, and only the pool's format marker tells a pooled archive's from a legacy one.
    if snapshot.local_path is not None:
        return snapshot
    return snapshot if snapshot.state == "evicted" and original_pool.enabled(vault) else None


def evicted(vault: Path, path: str | Path) -> bool:
    """Whether a path names an evicted original; an unbound vault and a note never do."""
    if binding_for(vault) is None:
        return False
    return not is_note_path(Path(path).as_posix()) and original_state(vault, path).state == "evicted"


def companion_paths(relative: str) -> tuple[str, ...]:
    """Every path a companion of this original may have; preserve owns the layout, legacy Source forms included."""
    from .governance.raw_protection import marked
    from .preserve import _sidecar_paths

    target = Path(relative)
    return tuple(
        candidate.as_posix()
        for candidate in _sidecar_paths(
            target, protected=marked(relative), source=target.is_relative_to(Path(managed_join("Sources"))),
        )
    )


def companion_candidates(
    vault: Path, relative: str, *, companions: Mapping[str, bytes] | None = None,
) -> Iterator[tuple[str, bytes]]:
    """Each companion candidate of this original that exists, as its path and bytes.

    A missing candidate is skipped; any other failed read raises. A caller that has already read every page
    that can be a companion, with one batch read, passes them as ``companions`` by vault-relative path; a
    candidate absent from it is missing.
    """
    from . import reserved_paths

    for companion in companion_paths(relative):
        if companions is not None:
            if (held := companions.get(companion)) is not None:
                yield companion, held
            continue
        try:
            data = reserved_paths.read_generic_bytes(vault, companion).data
        except reserved_paths.ReservedPathLeafError as error:
            if error.code != "MISSING":
                raise
            continue
        yield companion, data


def bound_companion(
    vault: Path, relative: str, *, companions: Mapping[str, bytes] | None = None,
) -> tuple[str, dict[str, object], bytes] | None:
    """Return the first companion candidate whose pointer fields all name this original.

    A malformed candidate raises UnicodeError or FrontmatterError; each caller decides what it means.
    """
    from .vault import parse_frontmatter

    for companion, data in companion_candidates(vault, relative, companions=companions):
        fields, _body, marker = parse_frontmatter(data.decode("utf-8"), custody=True)
        if marker is not None and bound_path(fields) == relative:
            return companion, fields, data
    return None


class ArchiveReads:
    """Archive-level state that one walk over many bindings reads once, as each binding first needs it."""

    def __init__(self, vault: Path) -> None:
        self.vault = vault
        self._binding: tuple[StoreBinding | None] | None = None
        self._pooled = False

    def binding(self) -> StoreBinding | None:
        if self._binding is None:
            self._binding = (binding_for(self.vault),)
        return self._binding[0]

    def pooled(self) -> bool:
        from . import original_pool

        # A published pool marker is never removed, so a True read is kept. A missing marker is read again, so a
        # walk sees a conversion that starts while it runs.
        if not self._pooled:
            self._pooled = original_pool.enabled(self.vault)
        return self._pooled


def original_state(
    vault: Path, path: str | Path, *, binding: StoreBinding | None = None,
    companions: Mapping[str, bytes] | None = None, archive: ArchiveReads | None = None,
) -> OriginalSnapshot:
    """Resolve local, cached, evicted or missing from one exact sibling binding.

    This reader never constructs a store client or opens the job database.
    Foreign references deliberately confer no presence in the selected vault.
    ``companions`` is ``companion_candidates``'s: pages that the caller has already read. A walk passes one
    ``archive`` for all its bindings, so it reads the store binding and the pool marker once.
    """
    from . import reserved_paths
    from .vault import FrontmatterError
    from .vault_store import read as store_read

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
    if store_read.serves(vault, relative):
        # The store decides whether a page it owns exists; the file at its path is a projection.
        physical = store_read.page_exists(vault, relative)
    else:
        try:
            reserved_paths.inspect_generic_file(vault, relative)
            physical = True
        except reserved_paths.ReservedPathLeafError as error:
            if error.code != "MISSING":
                raise
            physical = False
    try:
        found = bound_companion(vault, relative, companions=companions)
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
    local_path = None
    if not physical and not store_read.serves(vault, relative) and may_pool(relative):
        from . import original_pool

        try:
            pooled = archive.pooled() if archive is not None else original_pool.enabled(vault)
            present = pooled and original_pool.locally_present(
                vault, binary_binding_value.sha256, binary_binding_value.size)
        except original_pool.ObjectMismatch:
            present = True  # A damaged object still holds the filing's place; each read of it refuses.
        if present:
            from .trash_metadata import retired_companion

            # A companion retired to Trash stays at its path, but it files nothing: equal bytes that another
            # filing pools are not its own.
            present = not retired_companion(vault, companion, fields, hashlib.sha256(data).hexdigest())
        if present:
            physical = True
            local_path = original_pool.object_path(vault, binary_binding_value.sha256)
    reference = stored_reference(fields.get("stored_object"))
    selected = binding if binding is not None else archive.binding() if archive is not None else binding_for(vault)
    if selected is None or (reference is not None and reference.store != selected.store):
        reference = None
    state = ("cached" if physical else "evicted") if reference is not None else ("local" if physical else "missing")
    return OriginalSnapshot(
        state, binary, sidecar, binary_binding_value.sha256, binary_binding_value.size,
        reference, hashlib.sha256(data).hexdigest(),
        # Datasets stay on the volume with notes (design D1); `data_file` is their closed pointer field. So does a
        # suffix-less original of a pooled archive, whose evicted bytes could otherwise turn pool-only (`may_share_bytes`).
        offloadable=not is_note_path(relative) and "data_file" not in fields
        and not resident_suffixless(vault, relative, archive),
        local_path=local_path,
        replicas=replica_references(fields.get(REPLICAS)),
    )


def resident_suffixless(vault: Path, relative: str, archive: ArchiveReads | None = None) -> bool:
    """Whether an original named `relative` stays on the volume, never offloaded, because a pooled archive
    keeps a suffix-less one resident (`may_share_bytes`). The one rule that offload and upload admission share."""
    if may_share_bytes(relative):
        return False
    from . import original_pool

    return archive.pooled() if archive is not None else original_pool.enabled(vault)


def evicted_visible_original(vault: Path, path: str) -> tuple[Path, str, OriginalSnapshot] | None:
    """An evicted original that the caller may see, found without fetching it; any other absent path is none.

    A door that moves an original reads this to accept the path before it materializes anything, so a
    withheld original and an absent path answer alike whether the store is reachable or not.
    """
    if binding_for(vault) is None:
        return None
    from . import reserved_paths
    from .vault import VaultPathError, resolve_under_vault

    try:
        abs_path, rel_path = resolve_under_vault(vault, path, must_be_file=True)
        stored = original_state(vault, abs_path)
    except (VaultPathError, reserved_paths.ReservedPathLeafError):
        return None
    if stored.state != "evicted" or not caller_may_see(vault, rel_path):
        return None
    return abs_path, rel_path, stored


def caller_may_see(vault: Path, relative: str) -> bool:
    """Whether a door that moves an original with no file at its path may show it to this caller.

    A bound store filters the owner's walk too (it leaves out the trash); a door withholds nothing
    from the owner, as `egress.write_target_withheld` decides.
    """
    from .governance import egress

    if not egress.caller_restricted(vault):
        return True
    visible = egress.restricted_release_filter(vault)
    return visible is None or visible(relative)
