"""Document forms: what a captured document is as a record of a work.

A form is one entry of the `document-forms` registry: a shipped pack plus the vault overlay
`_Schema/document-forms.yaml`. `save_source` takes an optional form; a key the registry does not
know yet registers in the same atomic write that captures the Source, as a new source kind does.
Code never branches on a form key: a Source records the resolved key and readers show it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .source_taxonomy import derive_label
from .vault import PathGuard, PlannedWrite, kb_root, read_guarded_text
from .vocabulary import registry

PACK = "document-forms.yaml"


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "document-forms.yaml"


def _check(entry: registry.Entry, entries: object) -> None:
    """A form carries labels only, so the generic entry rules are all there is to check."""


SPEC = registry.RegistrySpec(
    name="document-forms",
    stem="document-forms",
    overlay=registry_path,
    adapter=registry.FixedPackAdapter(PACK, lambda: SPEC, _check),
    fields=frozenset({"label", "description", "aliases", "status", "replaced_by"}),
    # Labels only: a form carries no behaviour, so a vault's new key never needs review.
    promotion="auto-register",
)


@dataclass(frozen=True, slots=True)
class FormPlan:
    """The key a capture records and the registry write that must land with it, if any."""

    key: str | None = None
    introduced: bool = False
    writes: tuple[PlannedWrite, ...] = ()
    #: The overlay text the writes were planned against; None when the overlay did not exist.
    source_text: str | None = None
    version: str | None = None
    selected_spec: registry.RegistrySpec | None = None

    def recheck_bindings(self, root: Path) -> None:
        if self.selected_spec is not None:
            from .vocabulary import instances

            instances.recheck(root, self.selected_spec)

    def receipt(self) -> list[str]:
        """The `vocabulary_receipt` line for a key this capture registered."""
        if not self.introduced or self.version is None or self.key is None:
            return []
        from .vocabulary import contract

        return contract.first_use_lines({SPEC.name: [self.key]}, self.version)


def plan(vault_root: Path, supplied: str | None) -> FormPlan:
    """Resolve a supplied form to its key and plan the registration of a new one.

    A key or alias in the registry resolves to its entry, following `replaced_by` when the entry is
    deprecated. A well-formed key that nothing names is new. A malformed key, or an overlay that
    does not parse, raises `RegistryError` and writes nothing.
    """
    if supplied is None or not supplied.strip():
        return FormPlan()
    from . import registry_history
    from .vocabulary import instances

    root = Path(vault_root)
    spec = instances.select(root, SPEC)
    snapshot = registry.load(spec, root)
    try:
        key = spec.adapter.normalize_key(supplied)
    except registry.RegistryError:
        raise registry.RegistryError(
            "INVALID_DOCUMENT_FORM: document_form starts with a letter and names the kind of record, "
            "such as full-text or citation-record"
        ) from None
    by_alias = {registry.token(alias): entry for entry in snapshot.entries.values() for alias in entry.aliases}
    known = snapshot.entries.get(key) or by_alias.get(key)
    if known is not None:
        replacement = snapshot.entries.get(known.replaced_by or "")
        resolved = replacement if known.status == "deprecated" and replacement is not None else known
        return FormPlan(key=resolved.key, selected_spec=spec)
    if snapshot.findings:
        raise registry.RegistryError(
            "INVALID_DOCUMENT_FORMS: fix _Schema/document-forms.yaml before a new form can register; "
            + str(snapshot.findings[0].get("detail") or snapshot.findings[0].get("code"))
        )

    instances.select(root, SPEC, spec.instance_id, authoring=True)
    instances.recheck(root, spec)
    path = spec.overlay(root)
    try:
        text, guard = read_guarded_text(root, path)
        existed = True
    except FileNotFoundError:
        text = None
        guard = PathGuard.capture(root, path.relative_to(root).as_posix(), leaf_policy="absent")
        existed = False
    document = spec.adapter.document(text)
    spec.adapter.put(document, key, registry.Entry(key=key, label=derive_label(key), origin="vault"), existing=False)
    spec.adapter.parse_document(document)
    updated = spec.adapter.render(document)
    version, kept = registry_history.plan_snapshot(
        root,
        path=path,
        stem=spec.stem,
        previous=text,
        operation="auto-register",
        before_hash=registry.content_hash(text) if existed else registry.NO_OVERLAY_HASH,
        after_hash=registry.content_hash(updated),
        rendered=updated,
        added={SPEC.name: (key,)},
    )
    write = PlannedWrite(path, updated, create_only=not existed, guard=guard)
    return FormPlan(key, True, (write, kept), text, version, spec)
