"""One loader and one contract for every vocabulary registry.

A registry is a shipped pack plus an optional vault overlay under
`<Knowledge Base>/_Schema/`. A `RegistrySpec` declares what differs between
registries: the pack, the overlay path, the history stem, which generic fields
and attributes a delta may carry, which of them are fixed once an entry exists,
whether a deprecation needs a replacement, the cap, and the review family a
restricted principal's save is queued under. Its adapter turns the overlay's
own grammar into the module's typed registry and into generic `Entry` rows,
and writes a generic entry back into that grammar.

Everything else lives here once: reading and caching, the two hashes, delta
application and its meaning rules, and the history-backed save and restore.

Two hashes, two jobs. `content_hash` is the sha256 of the overlay's UTF-8
text with universal newlines, or `none` without an overlay. It is the public
`expected_hash` checked by a save, and it
equals each module's existing hash. `effective_digest` is the sha256 of the
canonical effective entries, so two overlays that resolve to the same
vocabulary share it. `overlay_text` retains exact decoded bytes for the
canonical writer's independent raw-content guard and the kept snapshot.

The cache holds one snapshot per vault and registry. An unchanged file stat
returns it without a read; a changed stat reads the file and keeps the
snapshot when the exact text still matches. A governed save drops the entry, and
the file watcher drops it on a `_Schema/` event.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

import yaml

PACK_SCHEMA_VERSION = 1
NO_OVERLAY_HASH = "none"
#: The generic entry fields a delta may name. A spec narrows them.
ENTRY_FIELDS = frozenset(
    # nosemgrep: ep-word-set -- The generic registry entry schema defines these field names.
    {"label", "description", "aliases", "status", "replaced_by", "parent", "attributes", "guidance"}
)
# nosemgrep: ep-word-set -- The save protocol fixes these three operation names.
_DELTA_VERBS = frozenset({"upsert", "alias", "deprecate"})
_STATUSES = frozenset({"active", "deprecated"})
#: A save that would grow an overlay past this many bytes is refused.
MAX_OVERLAY_BYTES = 256 * 1024
# Collision comparison normalizes registry token spelling, never the meaning of prose.
_TOKEN_RE = re.compile(r"[^a-z0-9]+")


class RegistryError(ValueError):
    """A refused registry operation; the message starts with a stable code."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One registry entry in the generic shape every registry shares."""

    key: str
    label: str = ""
    description: str = ""
    aliases: tuple[str, ...] = ()
    status: str = "active"
    replaced_by: str | None = None
    parent: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    guidance: str = ""
    origin: str = "pack"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "status": self.status, "origin": self.origin}
        for name in ("label", "description", "guidance", "parent", "replaced_by"):
            value = getattr(self, name)
            if value:
                out[name] = value
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.attributes:
            out["attributes"] = _plain(self.attributes)
        return out


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One loaded registry: the typed registry, its entries and its identity."""

    registry: str
    typed: Any
    entries: Mapping[str, Entry]
    findings: tuple[Mapping[str, Any], ...]
    content_hash: str
    effective_digest: str
    overlay_text: str | None

    @property
    def active(self) -> tuple[Entry, ...]:
        return tuple(entry for entry in self.entries.values() if entry.status == "active")


class Adapter(Protocol):
    """What one registry supplies to the generic loader."""

    def parse(self, text: str | None, digest: str) -> Any:
        """The typed registry for an overlay text (`None` when there is no overlay)."""

    def parse_document(self, document: Mapping[str, Any]) -> Any:
        """The typed registry for an overlay document a save is about to write."""

    def entries(self, typed: Any) -> Mapping[str, Entry]:
        """Every effective entry, pack and vault, keyed by canonical key."""

    def findings(self, typed: Any) -> tuple[Mapping[str, Any], ...]:
        """The registry's validation findings."""

    def document(self, text: str | None) -> dict[str, Any]:
        """The overlay document a delta is applied to; an empty one without a file."""

    def put(self, document: dict[str, Any], key: str, entry: Entry, *, existing: bool) -> None:
        """Write one generic entry into the overlay document in its own grammar."""

    def render(self, document: Mapping[str, Any]) -> str:
        """The overlay text for a document."""

    def normalize_key(self, raw: str) -> str:
        """The canonical spelling of a key, or a `RegistryError`."""


@dataclass(frozen=True, slots=True)
class RegistrySpec:
    """What one registry declares; the loader and the contract do the rest."""

    name: str
    stem: str
    overlay: Callable[[Path], Path]
    adapter: Adapter
    #: Generic fields a delta may carry, and the attributes it may set.
    fields: frozenset[str]
    attributes: frozenset[str] = frozenset()
    #: Fields fixed once an entry exists: `parent` and `attributes.<name>` rows.
    immutable: frozenset[str] = frozenset()
    replacement_required: bool = False
    #: `propose-save` registries carry behaviour; `auto-register` ones only labels.
    promotion: str = "propose-save"
    #: The vocabulary review family a restricted principal's save is queued under.
    family: str | None = None
    #: The most vault entries an overlay may hold after a save.
    cap: int = 512
    #: How usage counts are read: `(vault_root, snapshot) -> Usage`.
    usage: Callable[[Path, Snapshot], Any] | None = None
    #: The bootstrap block that already lists this registry's values. The bootstrap
    #: code fixes the block names, so this is a closed attribute, not vocabulary.
    served_by: str | None = None


# --------------------------------------------------------------------------- #
# Packs
# --------------------------------------------------------------------------- #


def pack_text(name: str) -> str:
    """The shipped core pack's text, e.g. `pack_text("relations.yaml")`."""
    return files("exomem.vocabulary").joinpath("packs", "core", name).read_text(encoding="utf-8")


@cache
def pack_entries(name: str) -> tuple[Entry, ...]:
    """The entries of a generic-grammar pack, in pack order.

    A pack is product data: a malformed one is a build defect, so this raises
    instead of degrading to findings.
    """
    data = yaml.safe_load(pack_text(name))
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != PACK_SCHEMA_VERSION
        or not isinstance(data.get("entries"), dict)
    ):
        raise RuntimeError(f"vocabulary pack {name!r} is malformed")
    out: list[Entry] = []
    for key, raw in data["entries"].items():
        if not isinstance(raw, dict) or set(raw) - ENTRY_FIELDS:
            raise RuntimeError(f"vocabulary pack {name!r} entry {key!r} is malformed")
        out.append(
            Entry(
                key=str(key),
                label=str(raw.get("label") or ""),
                description=str(raw.get("description") or ""),
                aliases=tuple(str(item) for item in raw.get("aliases") or ()),
                status=str(raw.get("status") or "active"),
                replaced_by=raw.get("replaced_by"),
                parent=raw.get("parent"),
                attributes=MappingProxyType(dict(raw.get("attributes") or {})),
                guidance=str(raw.get("guidance") or ""),
                origin="pack",
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- #
# Loading and caching
# --------------------------------------------------------------------------- #


def content_hash(text: str) -> str:
    """Preserve the legacy public hash from a universal-newline UTF-8 read."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def effective_digest(registry: str, entries: Mapping[str, Entry]) -> str:
    # Provenance describes where an entry came from, not its effective meaning.
    effective = []
    for key in sorted(entries):
        entry = entries[key].as_dict()
        entry.pop("origin", None)
        effective.append(entry)
    payload = [registry, effective]
    return content_hash(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


@dataclass(slots=True)
class _Cached:
    stat: tuple[int, ...] | None
    snapshot: Snapshot


_CACHE: dict[tuple[str, str], _Cached] = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(vault_root: Path, spec: RegistrySpec) -> tuple[str, str]:
    return (os.path.abspath(vault_root), spec.name)


def _stat_key(path: Path) -> tuple[int, ...] | None:
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino)


def _build(spec: RegistrySpec, text: str | None) -> Snapshot:
    digest = content_hash(text) if text is not None else NO_OVERLAY_HASH
    typed = spec.adapter.parse(text, digest)
    entries = MappingProxyType(dict(spec.adapter.entries(typed)))
    return Snapshot(
        registry=spec.name,
        typed=typed,
        entries=entries,
        findings=tuple(spec.adapter.findings(typed)),
        content_hash=digest,
        effective_digest=effective_digest(spec.name, entries),
        overlay_text=text,
    )


def load(spec: RegistrySpec, vault_root: Path | None) -> Snapshot:
    """The current snapshot of one registry for one vault (pack only without a vault)."""
    if vault_root is None:
        return _pack_only(spec)
    path = spec.overlay(Path(vault_root))
    key = _cache_key(vault_root, spec)
    stat = _stat_key(path)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None and stat is not None and cached.stat == stat:
        return cached.snapshot
    if stat is None:
        snapshot = _pack_only(spec)
    else:
        try:
            text: str | None = path.read_bytes().decode("utf-8")
        except (FileNotFoundError, NotADirectoryError):
            text = None
        if cached is not None and cached.snapshot.overlay_text == text:
            snapshot = cached.snapshot
        else:
            snapshot = _build(spec, text)
        # A write between the two stats leaves no stat key, so the next load
        # reads the file again instead of trusting bytes it may have missed.
        after = _stat_key(path) if text is not None else None
        stat = after if after == stat else None
    with _CACHE_LOCK:
        _CACHE[key] = _Cached(stat, snapshot)
    return snapshot


def cached(spec: RegistrySpec, vault_root: Path) -> Snapshot | None:
    """The snapshot cached for one vault's registry, without loading; None if absent."""
    with _CACHE_LOCK:
        entry = _CACHE.get(_cache_key(vault_root, spec))
    return None if entry is None else entry.snapshot


_PACK_ONLY: dict[str, Snapshot] = {}


def _pack_only(spec: RegistrySpec) -> Snapshot:
    snapshot = _PACK_ONLY.get(spec.name)
    if snapshot is None:
        snapshot = _PACK_ONLY[spec.name] = _build(spec, None)
    return snapshot


def invalidate(vault_root: Path | None = None, *, path: Path | str | None = None) -> None:
    """Drop cached snapshots: all of them, one vault's, or those reading `path`."""
    with _CACHE_LOCK:
        if vault_root is None:
            _CACHE.clear()
            return
        root = os.path.abspath(vault_root)
        for cache_key in [key for key in _CACHE if key[0] == root]:
            if path is None:
                _CACHE.pop(cache_key, None)
                continue
            spec = _specs().get(cache_key[1])
            if spec is None or os.path.abspath(spec.overlay(Path(root))) == os.path.abspath(
                Path(root) / path if not Path(path).is_absolute() else path
            ):
                _CACHE.pop(cache_key, None)


def clear_cache() -> None:
    invalidate()
    _PACK_ONLY.clear()


def invalidate_registry(name: str) -> None:
    """Drop every vault's cached snapshot of one registry."""
    with _CACHE_LOCK:
        for cache_key in [key for key in _CACHE if key[1] == name]:
            _CACHE.pop(cache_key, None)


def _specs() -> Mapping[str, RegistrySpec]:
    from . import registry_specs

    return registry_specs()


# --------------------------------------------------------------------------- #
# Deltas
# --------------------------------------------------------------------------- #


def token(value: object) -> str:
    """The comparison token for collision checks: case and punctuation folded."""
    return _TOKEN_RE.sub("-", str(value or "").strip().casefold()).strip("-")


def _string_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RegistryError(f"INVALID_REGISTRY_DELTA: {where} must be a list of strings")
    return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))


def _patch(spec: RegistrySpec, key: str, raw: object, base: Entry | None) -> Entry:
    """A generic entry built from a delta row over `base` (the existing entry)."""
    if not isinstance(raw, Mapping):
        raise RegistryError(f"INVALID_REGISTRY_DELTA: upsert.{key} must be an object")
    unknown = sorted(set(raw) - spec.fields)
    if unknown:
        raise RegistryError(
            f"INVALID_REGISTRY_DELTA: upsert.{key} has fields {spec.name} does not take: "
            + ", ".join(unknown)
            + f"; it takes {', '.join(sorted(spec.fields))}"
        )
    attributes = raw.get("attributes", {})
    if not isinstance(attributes, Mapping):
        raise RegistryError(f"INVALID_REGISTRY_DELTA: upsert.{key}.attributes must be an object")
    unknown_attributes = sorted(set(attributes) - spec.attributes)
    if unknown_attributes:
        raise RegistryError(
            f"INVALID_REGISTRY_DELTA: upsert.{key}.attributes has names {spec.name} does not "
            "take: "
            + ", ".join(unknown_attributes)
            + (f"; it takes {', '.join(sorted(spec.attributes))}" if spec.attributes else "")
        )
    for name in ("label", "description", "guidance", "parent", "replaced_by", "status"):
        if name in raw and raw[name] is not None and not isinstance(raw[name], str):
            raise RegistryError(f"INVALID_REGISTRY_DELTA: upsert.{key}.{name} must be a string")
    status = str(raw.get("status") or (base.status if base else "active"))
    if status not in _STATUSES:
        raise RegistryError(
            f"INVALID_REGISTRY_DELTA: upsert.{key}.status must be active or deprecated"
        )
    aliases = _string_list(raw["aliases"], f"upsert.{key}.aliases") if "aliases" in raw else ()
    merged_attributes = dict(base.attributes) if base else {}
    merged_attributes.update(dict(attributes))
    return Entry(
        key=key,
        label=str(raw["label"]).strip() if raw.get("label") else (base.label if base else ""),
        description=(
            str(raw["description"]).strip()
            if raw.get("description")
            else (base.description if base else "")
        ),
        aliases=tuple(dict.fromkeys([*(base.aliases if base else ()), *aliases])),
        status=status,
        replaced_by=(
            str(raw["replaced_by"]).strip()
            if raw.get("replaced_by")
            else (base.replaced_by if base else None)
        ),
        parent=str(raw["parent"]).strip() if raw.get("parent") else (base.parent if base else None),
        attributes=MappingProxyType(merged_attributes),
        guidance=str(raw["guidance"]).strip()
        if raw.get("guidance")
        else (base.guidance if base else ""),
        origin="vault",
    )


def _check_in_place(spec: RegistrySpec, before: Entry, after: Entry) -> None:
    """Meaning is never rewritten in place: the rules an existing entry keeps."""
    changed: list[str] = []
    if "parent" in spec.immutable and before.parent != after.parent:
        changed.append("parent")
    if "description" in spec.immutable and before.description != after.description:
        changed.append("description")
    for name in sorted(spec.attributes):
        if f"attributes.{name}" in spec.immutable and (
            _plain(before.attributes.get(name)) != _plain(after.attributes.get(name))
        ):
            changed.append(f"attributes.{name}")
    if changed:
        raise RegistryError(
            f"IMMUTABLE_REGISTRY_MEANING: {after.key} keeps its "
            + ", ".join(changed)
            + "; promote a new key and deprecate this one instead"
        )
    if before.status == "deprecated" and (
        after.status != "deprecated" or after.replaced_by != before.replaced_by
    ):
        raise RegistryError(
            f"IMMUTABLE_REPLACEMENT: {after.key} is deprecated; "
            "its status and replacement are fixed"
        )


def apply_delta(
    spec: RegistrySpec, snapshot: Snapshot, delta: object
) -> tuple[dict[str, Any], dict[str, Entry]]:
    """The overlay document after a delta, and each touched key's proposed entry.

    Pure: nothing is written. The caller validates the resulting registry,
    which may drop a proposed entry that fails validation; the proposed entry
    is still returned so a caller can say what it collides with.
    """
    if not isinstance(delta, Mapping) or not delta or set(delta) - _DELTA_VERBS:
        raise RegistryError(
            "INVALID_REGISTRY_DELTA: a delta has upsert, alias and/or deprecate, "
            'e.g. {"upsert": {"<key>": {...}}}'
        )
    document = spec.adapter.document(snapshot.overlay_text)
    touched: list[str] = []
    working = dict(snapshot.entries)

    def canonical(raw_key: object, verb: str) -> str:
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise RegistryError(f"INVALID_REGISTRY_DELTA: {verb} keys must be strings")
        key = spec.adapter.normalize_key(raw_key)
        if key != raw_key:
            raise RegistryError(f"INVALID_REGISTRY_KEY: {raw_key!r} is not canonical; use {key!r}")
        return key

    upserts = delta.get("upsert", {})
    if not isinstance(upserts, Mapping):
        raise RegistryError("INVALID_REGISTRY_DELTA: upsert must be an object")
    for raw_key, raw in upserts.items():
        key = canonical(raw_key, "upsert")
        base = working.get(key)
        entry = _patch(spec, key, raw, base)
        if base is not None:
            if base.aliases and not set(base.aliases) <= set(entry.aliases):
                raise RegistryError(f"IMMUTABLE_ALIASES: {key} aliases may only be added")
            _check_in_place(spec, base, entry)
        spec.adapter.put(document, key, entry, existing=base is not None)
        working[key] = entry
        touched.append(key)

    aliases = delta.get("alias", {})
    if not isinstance(aliases, Mapping):
        raise RegistryError("INVALID_REGISTRY_DELTA: alias must be an object")
    for raw_key, raw in aliases.items():
        key = canonical(raw_key, "alias")
        base = working.get(key)
        if base is None:
            raise RegistryError(
                f"UNKNOWN_REGISTRY_KEY: alias names {key!r}, which is not registered"
            )
        added = _string_list(raw, f"alias.{key}")
        entry = _patch(spec, key, {"aliases": list(added)}, base)
        spec.adapter.put(document, key, entry, existing=True)
        working[key] = entry
        touched.append(key)

    deprecations = delta.get("deprecate", {})
    if not isinstance(deprecations, Mapping):
        raise RegistryError("INVALID_REGISTRY_DELTA: deprecate must be an object")
    for raw_key, replacement in deprecations.items():
        key = canonical(raw_key, "deprecate")
        base = working.get(key)
        if base is None:
            raise RegistryError(
                f"UNKNOWN_REGISTRY_KEY: deprecate names {key!r}, which is not registered"
            )
        if replacement is not None and not isinstance(replacement, str):
            raise RegistryError(
                f"INVALID_REGISTRY_DELTA: deprecate.{key} names a replacement key or null"
            )
        if spec.replacement_required and not replacement:
            raise RegistryError(
                f"MISSING_REPLACEMENT: deprecating {key} needs an active replacement"
            )
        if base.status == "deprecated":
            if base.replaced_by != (replacement or None):
                raise RegistryError(
                    f"IMMUTABLE_REPLACEMENT: {key} is deprecated; its replacement is fixed"
                )
            continue
        entry = Entry(
            **{
                **_fields(base),
                "status": "deprecated",
                "replaced_by": replacement or None,
                "origin": "vault",
            }
        )
        spec.adapter.put(document, key, entry, existing=True)
        working[key] = entry
        touched.append(key)
    return document, {key: working[key] for key in dict.fromkeys(touched)}


def validate_replacements(spec: RegistrySpec, entries: Mapping[str, Entry]) -> None:
    """Every explicit replacement chain must end in an active final entry."""
    for entry in entries.values():
        if entry.status != "deprecated":
            continue
        if not entry.replaced_by:
            if spec.replacement_required:
                raise RegistryError(
                    f"MISSING_REPLACEMENT: deprecating {entry.key} needs an active replacement"
                )
            continue
        survivor = entry
        seen: set[str] = set()
        while survivor.status == "deprecated":
            replacement = survivor.replaced_by
            if survivor.key in seen or replacement is None or replacement not in entries:
                raise RegistryError(
                    f"INVALID_REPLACEMENT: {entry.key} must be replaced by a chain that ends "
                    "in an active entry"
                )
            seen.add(survivor.key)
            survivor = entries[replacement]


def added_keys(before: Mapping[str, Entry], after: Mapping[str, Entry]) -> tuple[str, ...]:
    """Keys active in `after` that were not active in `before`."""
    was_active = {key for key, entry in before.items() if entry.status == "active"}
    return tuple(
        sorted(
            key
            for key, entry in after.items()
            if entry.status == "active" and key not in was_active
        )
    )


def _fields(entry: Entry) -> dict[str, Any]:
    return {name: getattr(entry, name) for name in Entry.__slots__}


def new_findings(
    before: Snapshot | None, after_findings: tuple[Mapping[str, Any], ...]
) -> list[dict[str, Any]]:
    """Blocking findings the save introduces.

    A finding the current overlay already carries, such as a hand edit's, never
    refuses a save that does not touch it: the save still snapshots those bytes.
    """
    known = {_finding_identity(item) for item in (before.findings if before else ())}
    return [
        dict(item)
        for item in after_findings
        if item.get("severity", "error") == "error" and _finding_identity(item) not in known
    ]


def _finding_identity(item: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(item.get("code")), str(item.get("path")), str(item.get("detail")))


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


# --------------------------------------------------------------------------- #
# History-backed writes
# --------------------------------------------------------------------------- #


def commit(
    spec: RegistrySpec,
    vault_root: Path,
    rendered: str,
    *,
    operation: str,
    why: str | None,
    before_hash: str,
    previous: str | None,
    added: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Write one overlay with its snapshot and `log.md` entry as one batch."""
    from .. import registry_history

    root = Path(vault_root)
    path = spec.overlay(root)
    try:
        history = registry_history.commit(
            root,
            path=path,
            stem=spec.stem,
            rendered=rendered,
            operation=operation,
            why=why,
            before_hash=before_hash,
            previous=previous,
            after_hash=content_hash(rendered),
            added={spec.name: added},
        )
    finally:
        invalidate(root, path=path)
    return history
