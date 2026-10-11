"""Governed page labels, public canonical meanings and one operation's basis."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .cli_ops import OpError
from .vault import kb_root
from .vocabulary import registry

# nosemgrep: ep-word-set -- The page lifecycle protocol fixes these five semantic classes.
CLASSES = frozenset({"live", "pending", "superseded", "retired", "abandoned"})


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "statuses.yaml"


@dataclass(frozen=True)
class StatusRegistry:
    entries: Mapping[str, registry.Entry]
    findings: tuple[Mapping[str, Any], ...] = ()


class _Adapter:
    def normalize_key(self, raw: str) -> str:
        key = registry.token(raw)
        if not key or len(key) > 64 or not key[0].isalpha():
            raise registry.RegistryError("INVALID_REGISTRY_KEY: status keys start with a letter")
        return key

    def document(self, text: str | None) -> dict[str, Any]:
        if text is None:
            return {"schema_version": 1, "entries": {}}
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise registry.RegistryError(
                "INVALID_REGISTRY_OVERLAY: statuses YAML is invalid"
            ) from error
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            # The status overlay schema fixes these two document fields.
            or set(value) - {"schema_version", "entries"}
            or not isinstance(value.get("entries", {}), dict)
        ):
            raise registry.RegistryError(
                "INVALID_REGISTRY_OVERLAY: expected schema_version and entries"
            )
        # Shared history represents a missing overlay with schema_version alone.
        return {**value, "entries": value.get("entries", {})}

    def parse(self, text: str | None, digest: str) -> StatusRegistry:
        try:
            return self.parse_document(self.document(text))
        except registry.RegistryError as error:
            return StatusRegistry(
                self.entries(self.parse_document(self.document(None))),
                (
                    {
                        "code": "invalid_status_registry",
                        "path": "entries",
                        "severity": "error",
                        "detail": str(error),
                    },
                ),
            )

    def parse_document(self, document: Mapping[str, Any]) -> StatusRegistry:
        entries = {entry.key: entry for entry in registry.pack_entries("statuses.yaml")}
        owners = {self.normalize_key(key): key for key in entries}
        for raw_key, row in document["entries"].items():
            if not isinstance(raw_key, str) or self.normalize_key(raw_key) != raw_key:
                raise registry.RegistryError("INVALID_REGISTRY_KEY: status key is not canonical")
            if raw_key in entries:
                raise registry.RegistryError(
                    "PACK_ENTRY_FIXED: canonical page statuses cannot be overridden"
                )
            entry = registry._patch(SPEC, raw_key, row, None)
            if (
                not isinstance(entry.attributes.get("class"), str)
                or entry.attributes["class"] not in CLASSES
            ):
                raise registry.RegistryError(
                    "INVALID_STATUS_CLASS: a status needs a lifecycle class"
                )
            for label in (entry.key, entry.label, *entry.aliases):
                if not label:
                    continue
                token = self.normalize_key(label)
                if token in owners and owners[token] != raw_key:
                    raise registry.RegistryError(
                        "STATUS_LABEL_COLLISION: status labels must be distinct"
                    )
                owners[token] = raw_key
            if entry.status == "active" and entry.replaced_by:
                raise registry.RegistryError(
                    "INVALID_REPLACEMENT: an active status cannot redirect"
                )
            entries[raw_key] = entry
        registry.validate_replacements(SPEC, entries)
        return StatusRegistry(entries)

    def entries(self, typed: StatusRegistry) -> Mapping[str, registry.Entry]:
        return typed.entries

    def findings(self, typed: StatusRegistry) -> tuple[Mapping[str, Any], ...]:
        return typed.findings

    def put(
        self, document: dict[str, Any], key: str, entry: registry.Entry, *, existing: bool
    ) -> None:
        if key in {item.key for item in registry.pack_entries("statuses.yaml")}:
            raise registry.RegistryError(
                "PACK_ENTRY_FIXED: canonical page statuses cannot be changed"
            )
        row = entry.as_dict()
        row.pop("key")
        row.pop("origin")
        document["entries"][key] = row

    def render(self, document: Mapping[str, Any]) -> str:
        return yaml.safe_dump(dict(document), allow_unicode=True, sort_keys=True)


# The registry save protocol fixes field names; authored status keys remain pack/overlay data.
SPEC = registry.RegistrySpec(
    name="statuses",
    stem="statuses",
    overlay=registry_path,
    adapter=_Adapter(),
    fields=frozenset({"label", "description", "aliases", "status", "replaced_by", "attributes"}),
    attributes=frozenset({"class"}),
    immutable=frozenset({"attributes.class"}),
    replacement_required=True,
)


class ClassificationUnavailable(OpError):
    """A dependent operation needs a page lifecycle class that no admitted definition supplies."""


@dataclass(frozen=True)
class Classification:
    lifecycle_class: str | None
    unregistered: bool = False

    def require(self) -> str:
        if self.lifecycle_class is None:
            raise ClassificationUnavailable(
                "STATUS_CLASSIFICATION_UNAVAILABLE",
                "Page status classification is unavailable.",
                "Use an admitted status definition before this dependent operation.",
            )
        return self.lifecycle_class

    @property
    def live(self) -> bool:
        return self.require() == "live"

    @property
    def carryable(self) -> bool:
        # Named carry permits both current and pending lifecycle classes.
        return self.require() in {"live", "pending"}

    @property
    def historical(self) -> bool:
        # Historical currency uses the superseded and retired lifecycle classes.
        return self.require() in {"superseded", "retired"}

    @property
    def recurrence_evidence(self) -> bool:
        # Abandonment retains authored evidence; pending and historical material do not.
        return self.require() in {"live", "abandoned"}


@dataclass
class Basis:
    """One operation's lazy, admitted registry; canonical labels need only the pack."""

    root: Path | None
    _snapshots: dict[tuple[str, str | None], registry.Snapshot] = field(default_factory=dict, init=False, repr=False)
    _unavailable: bool = field(default=False, init=False, repr=False)

    def classify(
        self, value: object, *, path: str | None = None,
        frontmatter: Mapping[str, Any] | None = None,
    ) -> Classification:
        if value is None or (isinstance(value, str) and not value.strip()):
            return Classification("live")
        if not isinstance(value, str):
            # Parser compatibility: structural validation owns malformed raw values.
            return Classification("live")
        key = registry.token(value)
        public = registry.load(SPEC, None)
        canonical = public.entries.get(key)
        if canonical is not None:
            return Classification(str(canonical.attributes["class"]))
        from .vocabulary import instances
        from .vocabulary.contract import admission_refusal

        try:
            if self.root is None:
                raise registry.RegistryError("REGISTRY_UNAVAILABLE")
            if path is not None and frontmatter is None:
                from . import find_corpus
                from .governance import egress

                if not egress.quick_page_visible(self.root, path):
                    raise registry.RegistryError("REGISTRY_UNAVAILABLE")
                page = find_corpus.parse_page(self.root / path, 0, self.root)
                if page is None:
                    raise registry.RegistryError("REGISTRY_UNAVAILABLE")
                frontmatter = page.frontmatter
            scope = instances.page_scope(self.root, path, dict(frontmatter or {})) if path is not None else None
            spec = instances.select(self.root, SPEC, scope)
            if admission_refusal(self.root, spec) is not None:
                raise registry.RegistryError("REGISTRY_UNAVAILABLE")
            snapshot = registry.load(spec, self.root)
            self._snapshots[(spec.instance_id, spec.binding_revision)] = snapshot
        except (ValueError, OSError):
            self._unavailable = True
            return Classification(None)
        if snapshot.findings:
            self._unavailable = True
            return Classification(None)
        entry = next(
            (
                entry
                for entry in snapshot.entries.values()
                if key
                in {
                    registry.token(entry.key),
                    registry.token(entry.label),
                    *(registry.token(alias) for alias in entry.aliases),
                }
            ),
            None,
        )
        if entry is None:
            return Classification("live", unregistered=True)
        while entry.status == "deprecated":
            entry = snapshot.entries[entry.replaced_by]
        return Classification(str(entry.attributes["class"]))

    def ordering(self, value: object, *, path: str | None = None) -> Classification | None:
        """Classify a label for presentation order, through the page's own instance.

        A rooted basis keeps the fail-closed refusal. A rootless basis has no
        overlay to admit, so a label it cannot resolve has no class and orders
        as neither live nor historical instead of refusing the result.
        """
        classification = self.classify(value, path=path)
        if classification.lifecycle_class is None and self.root is None:
            return None
        return classification

    @property
    def dependency(self) -> tuple[str, str]:
        if self._unavailable:
            return ("unavailable", "")
        if not self._snapshots:
            return ("public", registry.load(SPEC, None).effective_digest)
        # This closed dependency format binds operation-local instance witnesses, not authority.
        rows = sorted((instance, revision, snapshot.effective_digest)
                      for (instance, revision), snapshot in self._snapshots.items())
        return ("instances:v1", json.dumps(rows, separators=(",", ":")))

    def matches(self, dependency: tuple[str, str]) -> bool:
        """Re-admit each selected instance before reusing a lifecycle result."""
        if dependency[0] == "public":
            return dependency == ("public", registry.load(SPEC, None).effective_digest)
        if self.root is None or dependency[0] != "instances:v1":
            return False
        from .vocabulary import instances
        from .vocabulary.contract import admission_refusal

        try:
            for instance, revision, digest in json.loads(dependency[1]):
                spec = instances.select(self.root, SPEC, instance)
                if spec.binding_revision != revision or admission_refusal(self.root, spec) is not None:
                    return False
                snapshot = registry.load(spec, self.root)
                if snapshot.findings or snapshot.effective_digest != digest:
                    return False
                self._snapshots[(instance, revision)] = snapshot
        except (ValueError, TypeError, OSError):
            return False
        return True
