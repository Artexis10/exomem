"""Canonical S1 field admission, independent of row policy and RAW releases."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from types import MappingProxyType

from .. import record_formats
from ..governance import policy, raw_protection
from .connection import CollectionStoreError

# S1 fixes location and unresolved coverage as protection states, not a field-name vocabulary.
UNRESOLVED = "unresolved"
# Scalar admission never authorizes a container; only an owner or explicit subtree release does.
WHOLE_SUBTREE = object()


def whole_value(selection):
    """Whether every value of the declared shape is admitted, including scalar-array members."""
    return selection is True or selection is WHOLE_SUBTREE or (
        isinstance(selection, tuple) and whole_value(selection[0]))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _spec(spec):
    return {"type": spec.type, "classification": spec.classification, "depends_on": list(spec.depends_on),
            "offset": spec.offset,
            "properties": {name: _spec(child) for name, child in spec.properties.items()},
            "items": None if spec.items is None else _spec(spec.items)}


def mapping_classes(mapping, manifest, fmt):
    """Resolve source coverage structurally, including aliases introduced by the mapping."""
    def source_path(raw):
        return (raw,) if fmt == "csv" else tuple(raw.split("."))

    coverage = {source_path(path): declaration for path, declaration in mapping.get("coverage", {}).items()}
    protected = {path for path, declaration in coverage.items() if declaration["classification"] == "location"}
    fields = {target: source_path(source.get("from") if isinstance(source, dict) else source)
              for target, source in mapping.get("fields", {}).items()}
    result = {}
    time_mapping = mapping.get("time") or {}
    # The mapping grammar fixes these keys; all time outputs share the declared source dependencies.
    time_sources = [source_path(value) for source in time_mapping.get("from", [])
                    for key, value in source.items() if key in {"instant", "offset", "date"}]
    time_targets = [time_mapping[key] for key in ("instant", "offset", "local_date") if time_mapping.get(key)]

    def protect(source, spec):
        if spec.classification == "location":
            protected.add(source)
        for name, child in spec.properties.items():
            protect((*source, name), child)
        if spec.items is not None:
            protect(source, spec.items)

    # A schema classification also protects its mapped source subtree from differently named extracted aliases.
    for target, source in fields.items():
        if target in manifest.schema.fields:
            protect(source, manifest.schema.fields[target])
    for target in time_targets:
        if target in manifest.schema.fields:
            for source in time_sources:
                protect(source, manifest.schema.fields[target])

    def classify(path):
        if any(path[:len(parent)] == parent for parent in protected):
            return "location"
        return coverage[path]["classification"] if path in coverage else UNRESOLVED

    def descendants(target, source, spec):
        result[target] = classify(source)
        for name, child in spec.properties.items():
            descendants((*target, name), (*source, name), child)
        if spec.items is not None:
            descendants(target, source, spec.items)

    for target, source in fields.items():
        if target in manifest.schema.fields:
            descendants((target,), source, manifest.schema.fields[target])
        else:
            result[(target,)] = classify(source)
    classes = [classify(source) for source in time_sources]
    if classes:
        classification = ("location" if "location" in classes
                          else UNRESOLVED if UNRESOLVED in classes else None)
        for target in time_targets:
            result[(target,)] = classification
    return result


def canonical_basis(conn, manifest):
    mappings, declarations = {}, set()
    for (encoded,) in conn.execute("SELECT binding_json FROM import_jobs WHERE collection_id=?", (manifest.collection_id,)):
        mapping = json.loads(encoded)["mapping"]
        declarations.add(_json([mapping["format"], mapping["declared"]]))
        current = mapping_classes(mapping["declared"], manifest, mapping["format"])
        for name, classification in current.items():
            before = mappings.get(name)
            mappings[name] = (UNRESOLVED if UNRESOLVED in (before, classification)
                              else "location" if "location" in (before, classification) else None)
    store_id = conn.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    declaration = {name: _spec(spec) for name, spec in manifest.schema.fields.items()}
    digest = hashlib.sha256(b"exomem.collection-fields.v1\0" + _json([store_id, manifest.collection_id,
                                                                  declaration, sorted(mappings.items()), sorted(declarations)]).encode()).hexdigest()
    return {"version": 1, "store_id": store_id, "collection_id": manifest.collection_id,
            "classification_basis": digest}, mappings


@dataclass(frozen=True)
class FieldPlan:
    basis: dict
    fields: object
    manifest: object
    owner: bool
    grants: tuple

    @property
    def whole_fields(self):
        return frozenset(name for name, selection in self.fields.items() if whole_value(selection))

    def admits_path(self, path):
        chosen = self.fields
        for name in path.split("."):
            if chosen is WHOLE_SUBTREE:
                return True
            if chosen is True:
                return False
            if isinstance(chosen, tuple):
                if not name.isdecimal():
                    return False
                chosen = chosen[0]
            else:
                chosen = chosen.get(name) if chosen is not None else None
            if chosen is None:
                return False
        return True

    def binding(self, paths):
        requested = [(path,) if path in self.fields else tuple(path.split(".")) for path in paths]
        relevant = [grant for grant in self.grants if any(
            path[:len(entry)] == entry or entry[:len(path)] == path
            for path in requested for entry in (tuple(item["path"].split(".")) for item in grant.field_release["paths"]))]
        return [(grant.id, grant.content_hash, grant.field_release) for grant in relevant]


def resolve(operation, manifest):
    basis, mapped = canonical_basis(operation.conn, manifest)
    who = operation.who
    owner = raw_protection.is_owner(who) and raw_protection.has_unrestricted_access(operation.root, who)
    if owner:
        fields = {name: WHOLE_SUBTREE for name in manifest.schema.fields}
        grammar = record_formats.log_grammar_tokens(manifest)
        if grammar is not None and grammar.note_field is not None:
            fields[grammar.note_field] = WHOLE_SUBTREE
        return FieldPlan(basis, MappingProxyType(fields), manifest, True, ())
    valid, purpose = raw_protection._authority(operation.root, who)
    grants = tuple(grant for grant in operation.policy.release_grants if valid and grant.field_release is not None
                   and grant.to_audience == who.audience_id
                   and all(grant.field_release[key] == basis[key] for key in basis)
                   and grant.field_release["surface"] == who.surface
                   and grant.field_release["issuer_family"] == who.issuer_family
                   and grant.field_release["purpose"] == purpose)

    def released(path, subtree=False):
        # Segment identity keeps a literal dotted property distinct from the public nested-path grammar.
        return any((target == path and (not subtree or entry["subtree"])) or (
                    entry["subtree"] and path[:len(target)] == target)
                   for grant in grants for entry in grant.field_release["paths"]
                   for target in (tuple(entry["path"].split(".")),))

    def nested(spec):
        return [spec.classification, *(value for sub in spec.properties.values() for value in nested(sub)),
                *(nested(spec.items) if spec.items is not None else [])]

    def dependency(name, seen=(), derived=False):
        if name in seen or name not in manifest.schema.fields:
            return UNRESOLVED
        spec = manifest.schema.fields[name]
        own = "location" if spec.classification == "location" else mapped.get((name,))
        if own == "location":
            return own
        children = [dependency(other, (*seen, name), True) for other in (*spec.depends_on, *((spec.offset,) if spec.offset else ()))]
        if derived:
            # An open object can carry undeclared descendants; its alias stays private until the parent is classified.
            if spec.type in {"object", "array"} and own != "location":
                children.append(UNRESOLVED)
            children.extend(nested(spec))
            children.extend(value for path, value in mapped.items() if len(path) > 1 and path[0] == name)
        if own == UNRESOLVED or UNRESOLVED in children:
            return UNRESOLVED
        if own == "location" or "location" in children:
            return "location"
        return None

    def select(spec, path, inherited=None):
        dependencies = [dependency(name, derived=True) for name in spec.depends_on]
        classification = ("location" if "location" in (inherited, spec.classification, mapped.get(path))
                          else mapped.get(path) or inherited)
        if classification != "location":
            if UNRESOLVED in dependencies:
                return None
            if "location" in dependencies:
                classification = "location"
        if classification == "location" and released(path, subtree=spec.type in {"object", "array"}):
            return WHOLE_SUBTREE if spec.type in {"object", "array"} else True
        if spec.type == "object":
            chosen = {name: value for name, child in spec.properties.items()
                      if (value := select(child, (*path, name),
                                          "location" if classification == "location" else None)) is not None}
            return MappingProxyType(chosen) if chosen else None
        if spec.type == "array":
            child = select(spec.items, path, classification)
            return (child,) if child is not None else None
        return None if classification in (UNRESOLVED, "location") else True

    selected = {name: value for name, spec in manifest.schema.fields.items()
                if (value := select(spec, (name,), dependency(name))) is not None}
    visible = replace(manifest, schema=replace(manifest.schema,
                      fields=MappingProxyType({name: spec for name, spec in manifest.schema.fields.items() if name in selected}),
                      natural_key=tuple(name for name in manifest.schema.natural_key if name in selected)))
    # A false refusal hides the unresolved field from recipients; accepting could disclose a private subtree.
    return FieldPlan(basis, MappingProxyType(selected), visible, False, grants)


def public_basis(operation, manifest):
    plan = resolve(operation, manifest)
    if not plan.owner:
        return None
    return {**plan.basis, "path": manifest.path, "ref": manifest.path, "fields": list(manifest.schema.fields),
            "release": {"tool": "govern_memory", "operations": ["propose", "commit"],
                        "kind": "collection-fields", "governance_version": policy.GOVERNANCE_VERSION,
                        "revoke": {"operation": "revoke", "scope": "standing", "grant_id": "the committed grant id"}}}


def _validate_release_paths(manifest, release):
    for entry in release.field_release["paths"]:
        names = entry["path"].split(".")
        spec = manifest.schema.fields.get(names[0])
        for name in names[1:]:
            while spec is not None and spec.type == "array":
                spec = spec.items
            spec = spec.properties.get(name) if spec is not None else None
        if spec is None or (spec.type in {"object", "array"} and not entry["subtree"]):
            raise CollectionStoreError("INVALID_FIELD_RELEASE", "release needs declared paths and explicit subtree coverage")


def resolve_release_basis(root, release, *, validate_paths=True):
    """Read the owner-bound canonical subject even while its policy transition is prepared."""
    from .preview import bound_writer

    def read(operation):
        if not (raw_protection.is_owner(operation.who) and raw_protection.has_unrestricted_access(root, operation.who)):
            raise CollectionStoreError("COLLECTION_STORE_OWNER_REQUIRED", "field release is owner-only")
        # A prepared governance transition temporarily blocks ordinary row admission; its owner must still verify its basis.
        manifest = operation.field_manifest(release.field_release["collection_id"])
        if validate_paths:
            _validate_release_paths(manifest, release)
        if manifest.path != release.path or release.ref != manifest.path:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "field release collection differs")
        return canonical_basis(operation.conn, manifest)[0]

    writer = bound_writer(root)
    if writer is not None:
        with writer.read_snapshot():
            return read(writer._operation)
    from ..query_engine.runtime import read_session
    from .connection import store_path

    with read_session(root, store_path(root)) as session:
        return read(session._authorization)
