"""Planning values: the governed vocabulary of the Planning core fields.

A shipped pack holds the values Planning has always had, under keys of the
form `<field>.<value>`; a vault adds values in `_Schema/planning-values.yaml`.
Shipped values are fixed, so capture defaults always resolve. A status carries
its planning `class` and a kind carries its `parents`; consumers branch on
those attributes, never on a value.

A Planning item stores the bare value. A stored value stays readable after its
definition is deprecated or removed; `PlanningValues.find` lets a writer refuse
such a value only when a write introduces it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from .vault import kb_root
from .vocabulary import instances, registry

PACK = "planning-values.yaml"
# nosemgrep: ep-word-set -- The planning class protocol fixes these three classes.
CLASSES = frozenset({"open", "done", "dropped"})
# A value is a lowercase hyphenated token; this checks the key's shape, not its meaning.
_VALUE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "planning-values.yaml"


@cache
def _pack() -> Mapping[str, registry.Entry]:
    return {entry.key: entry for entry in registry.pack_entries(PACK)}


@cache
def _fields() -> tuple[str, ...]:
    """The Planning fields the pack governs, in pack order."""
    return tuple(dict.fromkeys(key.partition(".")[0] for key in _pack()))


@dataclass(frozen=True)
class PlanningValueRegistry:
    entries: Mapping[str, registry.Entry]
    findings: tuple[Mapping[str, Any], ...] = ()


class _Adapter:
    def normalize_key(self, raw: str) -> str:
        name, _, value = str(raw).strip().casefold().partition(".")
        if name not in _fields() or len(value) > 64 or not _VALUE.fullmatch(value):
            raise registry.RegistryError(
                "INVALID_REGISTRY_KEY: a Planning value key is <field>.<value>, with field one "
                f"of {', '.join(_fields())} and a lowercase hyphenated value"
            )
        return f"{name}.{value}"

    def document(self, text: str | None) -> dict[str, Any]:
        if text is None:
            return {"schema_version": 1, "entries": {}}
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise registry.RegistryError(
                "INVALID_REGISTRY_OVERLAY: planning-values YAML is invalid"
            ) from error
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            # The overlay schema fixes these two document fields.
            or set(value) - {"schema_version", "entries"}
            or not isinstance(value.get("entries", {}), dict)
        ):
            raise registry.RegistryError(
                "INVALID_REGISTRY_OVERLAY: expected schema_version and entries"
            )
        # Shared history represents a missing overlay with schema_version alone.
        return {**value, "entries": value.get("entries", {})}

    def parse(self, text: str | None, digest: str) -> PlanningValueRegistry:
        try:
            return self.parse_document(self.document(text))
        except registry.RegistryError as error:
            return PlanningValueRegistry(
                dict(_pack()),
                (
                    {
                        "code": "invalid_planning_values",
                        "path": "entries",
                        "severity": "error",
                        "detail": str(error),
                    },
                ),
            )

    def parse_document(self, document: Mapping[str, Any]) -> PlanningValueRegistry:
        entries = dict(_pack())
        for raw_key, row in document["entries"].items():
            if not isinstance(raw_key, str) or self.normalize_key(raw_key) != raw_key:
                raise registry.RegistryError(
                    "INVALID_REGISTRY_KEY: Planning value key is not canonical"
                )
            if raw_key in entries:
                raise registry.RegistryError(
                    "PACK_ENTRY_FIXED: shipped Planning values cannot be overridden"
                )
            entries[raw_key] = registry._patch(SPEC, raw_key, row, None)
        kinds = {key.partition(".")[2] for key in entries if key.startswith("kind.")}
        for key, entry in entries.items():
            if entry.origin == "pack":
                continue
            name = key.partition(".")[0]
            planning_class = entry.attributes.get("class")
            parents = entry.attributes.get("parents")
            # The status and kind fields are the ones whose values carry attributes.
            if (name == "status") != (planning_class is not None) or (
                planning_class is not None and planning_class not in CLASSES
            ):
                raise registry.RegistryError(
                    "INVALID_PLANNING_CLASS: a status takes class open, done or dropped; "
                    "other values take none"
                )
            if (name == "kind") != (parents is not None) or (
                parents is not None
                and (
                    not isinstance(parents, list)
                    or not all(isinstance(item, str) and item in kinds for item in parents)
                )
            ):
                raise registry.RegistryError(
                    "INVALID_PLANNING_PARENTS: a kind takes parents, a list of registered "
                    "kinds; other values take none"
                )
            if entry.replaced_by and entry.replaced_by.partition(".")[0] != name:
                raise registry.RegistryError(
                    "INVALID_REPLACEMENT: a Planning value is replaced on its own field"
                )
        registry.validate_replacements(SPEC, entries)
        return PlanningValueRegistry(entries)

    def entries(self, typed: PlanningValueRegistry) -> Mapping[str, registry.Entry]:
        return typed.entries

    def findings(self, typed: PlanningValueRegistry) -> tuple[Mapping[str, Any], ...]:
        return typed.findings

    def put(
        self, document: dict[str, Any], key: str, entry: registry.Entry, *, existing: bool
    ) -> None:
        if key in _pack():
            raise registry.RegistryError(
                "PACK_ENTRY_FIXED: shipped Planning values cannot be changed"
            )
        row = entry.as_dict()
        row.pop("key")
        row.pop("origin")
        document["entries"][key] = row

    def render(self, document: Mapping[str, Any]) -> str:
        return yaml.safe_dump(dict(document), allow_unicode=True, sort_keys=False)


# The registry save protocol fixes field names; Planning values stay pack and overlay data.
SPEC = registry.RegistrySpec(
    name="planning-values",
    stem="planning-values",
    overlay=registry_path,
    adapter=_Adapter(),
    fields=frozenset({"label", "description", "status", "replaced_by", "attributes"}),
    attributes=frozenset({"class", "parents"}),
    # A stored item's archive and hierarchy rules must not change under it.
    immutable=frozenset({"attributes.class", "attributes.parents"}),
    # The bootstrap planning block lists kinds, horizons, priorities and commitments.
    # Statuses and health reach an agent through `new` markers and
    # schema_memory(subject="planning-values", operation="inspect").
    summarize_keys=False,
)


class Unavailable(Exception):
    """Only an overlay this caller cannot admit could define the value."""


@dataclass
class PlanningValues:
    """One operation's Planning values: the pack, plus the overlay once admitted.

    `server_side` reads the overlay without the caller's admission. It is only
    for derived server state whose disclosure is decided when it is served, so
    that state does not depend on which caller wrote last.
    """

    root: Path | None
    server_side: bool = field(default=False, kw_only=True)
    _snapshot: registry.Snapshot | None = field(default=None, init=False, repr=False)
    _attempted: bool = field(default=False, init=False, repr=False)

    def _overlay(self) -> registry.Snapshot | None:
        if not self._attempted:
            from .vocabulary.contract import admission_refusal

            self._attempted = True
            if self.root is None:
                return None
            # Admit the instance the read selects. A selection or binding error makes
            # the vault's values unavailable, as the bootstrap vocabulary block does.
            try:
                spec = instances.select(self.root, SPEC)
                if self.server_side or admission_refusal(self.root, spec) is None:
                    snapshot = registry.load(spec, self.root)
                    self._snapshot = None if snapshot.findings else snapshot
            except registry.RegistryError:
                self._snapshot = None
        return self._snapshot

    def find(self, name: str, value: object) -> registry.Entry | None:
        """The definition of one field value, or None when it is not registered.

        Raises `Unavailable` when the value is not shipped and the vault's
        definitions are withheld from this caller or invalid.
        """
        if type(value) is not str:
            return None
        key = f"{name}.{value}"
        entry = _pack().get(key)
        if entry is not None or self.root is None:
            return entry
        snapshot = self._overlay()
        if snapshot is None:
            raise Unavailable(key)
        return snapshot.entries.get(key)

    def fields(self) -> tuple[str, ...]:
        """The Planning fields the registry governs."""
        return _fields()

    def planning_class(self, status: object) -> str | None:
        """A status's planning class; None when it is unregistered or unavailable."""
        try:
            entry = self.find("status", status)
        except Unavailable:
            return None
        return None if entry is None else str(entry.attributes["class"])

    def values(self, name: str) -> tuple[str, ...]:
        """One field's active values in registry order: shipped first, then the vault's."""
        entries: Mapping[str, registry.Entry] = _pack()
        if self.root is not None:
            snapshot = self._overlay()
            if snapshot is not None:
                entries = snapshot.entries
        prefix = f"{name}."
        return tuple(
            key.removeprefix(prefix)
            for key, entry in entries.items()
            if key.startswith(prefix) and entry.status == "active"
        )

    @property
    def dependency(self) -> tuple[str, str]:
        if self._attempted and self.root is not None:
            return (
                ("effective", self._snapshot.effective_digest)
                if self._snapshot
                else ("unavailable", "")
            )
        return ("public", registry.load(SPEC, None).effective_digest)

    def matches(self, dependency: tuple[str, str]) -> bool:
        """Re-admit before comparing a cached result's private dependency."""
        if dependency[0] == "public":
            return dependency == ("public", registry.load(SPEC, None).effective_digest)
        self._attempted = False
        self._snapshot = None
        self._overlay()
        return dependency == self.dependency
