"""Exact code-owned auxiliary declarations for the vocabulary writer gate."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_ROLES = frozenset(  # nosemgrep: ep-word-set -- The sealed vocabulary writer manifest defines these code-owned output roles.
    {
        "relation-review",
        "lifecycle-review",
        "source-backref",
        "index",
        "operation-log",
        # The snapshot of the bytes a registry save replaced (`registry_history`).
        "registry-history",
        "compiled-history",  # The semantic writer retains its exact prior Markdown.
    }
)
# Primary-bound roles are admitted through their one primary (`vault._validate_batch_write_access`).
PRIMARY_BOUND_ROLES = frozenset(  # nosemgrep: ep-word-set -- Two of the sealed manifest's own code-owned roles.
    {"lifecycle-review", "compiled-history"}
)
_SEAL = object()


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _relative(root: Path, path: Path) -> str:
    try:
        return Path(path).absolute().relative_to(Path(root).absolute()).as_posix()
    except ValueError as exc:
        raise ValueError("VOCABULARY_AUXILIARY_PATH_INVALID") from exc


@dataclass(frozen=True, slots=True)
class DerivedAuxiliary:
    role: str
    path: str
    before_sha256: str | None
    after_sha256: str


@dataclass(frozen=True, slots=True)
class DerivedAuxiliaryManifest:
    root: Path
    primary_path: str
    primary_after_sha256: str
    entries: tuple[DerivedAuxiliary, ...]
    _seal: object = field(repr=False, compare=False, default=None)
    primary_before_sha256: str | None = None
    primary_before_source: str | None = field(repr=False, default=None)
    primary_guard: Any = field(repr=False, default=None)


def seal(
    vault_root: Path,
    *,
    primary: Any,
    derived: Iterable[tuple[str, Any]],
) -> DerivedAuxiliaryManifest:
    """Bind exact guarded derived outputs to one primary planned write."""
    root = Path(vault_root).resolve()
    if not isinstance(getattr(primary, "content", None), str):
        raise ValueError("VOCABULARY_AUXILIARY_CONTENT_INVALID")
    primary_path = _relative(root, Path(primary.path))
    entries: list[DerivedAuxiliary] = []
    for role, write in derived:
        guard = getattr(write, "guard", None)
        if role not in _ROLES or not isinstance(getattr(write, "content", None), str) or guard is None:
            raise ValueError("VOCABULARY_AUXILIARY_INVALID")
        path = _relative(root, Path(write.path))
        if path == primary_path:
            raise ValueError("VOCABULARY_AUXILIARY_PRIMARY_COLLISION")
        if role == "compiled-history":
            from . import compiled_history

            compiled_history.validate_new_write(write, path=path)
        if guard.leaf_policy == "absent":
            before = None
        elif guard.leaf_policy == "content" and isinstance(guard.expected_content_hash, str):
            before = guard.expected_content_hash
        else:
            raise ValueError("VOCABULARY_AUXILIARY_GUARD_INVALID")
        after = _hash(write.content)
        # The batch seam suppresses byte-identical planned writes. A claim for
        # one would have no staged image to bind, so omit it before sealing.
        if before == after:
            continue
        entries.append(DerivedAuxiliary(role, path, before, after))
    if len({item.path for item in entries}) != len(entries):
        raise ValueError("VOCABULARY_AUXILIARY_DUPLICATE")
    guard = getattr(primary, "guard", None)
    before = guard.expected_content_hash if guard is not None and guard.leaf_policy == "content" else None
    before_source = None
    if before is not None:
        from . import vault

        guard.recheck(root)
        before_source, _ = vault.read_guarded_text(root, primary.path)
        if _hash(before_source) != before:
            raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
        guard.recheck(root)
    return DerivedAuxiliaryManifest(
        root, primary_path, _hash(primary.content), tuple(entries), _SEAL, before, before_source, guard,
    )


def planned_images(vault_root: Path, writes: Iterable[Any]) -> tuple[Any, ...]:
    """Adapt complete guarded plans to the same validator as staged images."""
    root = Path(vault_root).resolve()
    images = []
    for write in writes:
        try:
            path = _relative(root, write.path)
        except ValueError:
            continue  # The ordinary batch seam checks external-state placement.
        if not isinstance(write.content, str):
            continue
        guard = write.guard
        images.append(SimpleNamespace(
            path=path,
            after=write.content.encode("utf-8"),
            before_sha256=(
                guard.expected_content_hash
                if guard is not None and guard.leaf_policy == "content" else None
            ),
            after_sha256=_hash(write.content),
        ))
    return tuple(images)


def _validate_each(vault_root: Path, images: tuple[Any, ...], manifests: tuple[Any, ...]) -> dict[str, str]:
    """Validate sealed bindings of independent primaries, each against the complete images.

    A claim is exclusive to one primary: a path that two bindings claim, or that is another
    binding's primary, is refused. The merged roles name every derived output once.
    """
    roles: dict[str, str] = {}
    primaries: set[str] = set()
    for manifest in manifests:
        if not isinstance(manifest, DerivedAuxiliaryManifest) or manifest.primary_path in primaries:
            raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
        primaries.add(manifest.primary_path)
        claimed = validate(vault_root, images, manifest)
        if PRIMARY_BOUND_ROLES.intersection(claimed.values()) or roles.keys() & claimed.keys():
            raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
        roles.update(claimed)
    if primaries & roles.keys():
        raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
    return roles


def validate(vault_root: Path, images: Iterable[Any], manifest: Any) -> dict[str, str]:
    """Validate one sealed binding, or a tuple of them, against the complete planned or staged images."""
    if manifest is None:
        return {}
    if isinstance(manifest, tuple):
        return _validate_each(Path(vault_root).resolve(), tuple(images), manifest)
    root = Path(vault_root).resolve()
    if not isinstance(manifest, DerivedAuxiliaryManifest) or manifest._seal is not _SEAL or manifest.root != root:
        raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
    by_path = {}
    for image in images:
        if image.path in by_path:
            raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
        by_path[image.path] = image
    primary = by_path.get(manifest.primary_path)
    if (
        primary is None
        or primary.after_sha256 != manifest.primary_after_sha256
        or primary.before_sha256 != manifest.primary_before_sha256
        or hashlib.sha256(primary.after).hexdigest() != manifest.primary_after_sha256
    ):
        raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
    roles: dict[str, str] = {}
    for claim in manifest.entries:
        image = by_path.get(claim.path)
        if (
            image is None or claim.role not in _ROLES or claim.path == manifest.primary_path
            or claim.path in roles or image.before_sha256 != claim.before_sha256
            or image.after_sha256 != claim.after_sha256
            or hashlib.sha256(image.after).hexdigest() != claim.after_sha256
        ):
            raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
        roles[claim.path] = claim.role
        if claim.role == "compiled-history":
            from . import compiled_history, vault

            guard = manifest.primary_guard
            if (
                claim.before_sha256 is not None
                or not isinstance(manifest.primary_before_source, str)
                or not isinstance(guard, vault.PathGuard)
                or guard.target != manifest.primary_path or guard.leaf_policy != "content"
                or guard.expected_content_hash != manifest.primary_before_sha256
                or _hash(manifest.primary_before_source) != manifest.primary_before_sha256
            ):
                raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
            compiled_history.validate_new_entry(
                root, path=claim.path, raw=image.after, primary_path=manifest.primary_path,
                before_source=manifest.primary_before_source, after_source=primary.after.decode("utf-8"),
            )
    lifecycle = tuple(path for path, role in roles.items() if role == "lifecycle-review")
    if lifecycle:
        from . import find_corpus, relation_review, semantic_contract
        from .memory_refs import ID_FIELD, normalize_id

        page = find_corpus.parse_page(root / manifest.primary_path, 0, root, content=primary.after)
        identity = normalize_id(page.frontmatter.get(ID_FIELD)) if page is not None else None
        if identity is None:
            raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
        slot = relation_review.lifecycle_prepared_path(root, identity).relative_to(root).as_posix()
        if slot not in lifecycle:
            raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
        prepared = relation_review._parse_lifecycle_prepared(by_path[slot].after, page_identity=identity)
        fingerprint = semantic_contract.review_content_fingerprint(identity, primary.after.decode("utf-8"))
        if (
            prepared.before_path != manifest.primary_path or prepared.after_path != manifest.primary_path
            or manifest.primary_before_source is None
            or prepared.before_source_hash != _hash(manifest.primary_before_source)
            or prepared.after_source_hash != manifest.primary_after_sha256
            or prepared.after_fingerprint != fingerprint
        ):
            raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
        for path in lifecycle:
            if path == slot:
                continue
            if path != prepared.decision_reference:
                raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
            decision = relation_review._parse_lifecycle_decision(
                by_path[path].after, page_identity=identity, after_fingerprint=fingerprint,
            )
            if (
                decision.reference != path
                or by_path[path].after_sha256 != prepared.decision_bytes_hash
            ):
                raise ValueError("VOCABULARY_AUXILIARY_CHANGED")
    return roles


def primary_admission(
    vault_root: Path, *, primary_path: str, current_source: str, proposed_source: str,
    primary_guard: Any, require_identity: bool = False,
) -> Callable[[str], bool]:
    """Admit bound auxiliary paths without inspecting candidate leaves.

    This prevents denied siblings from influencing edits. A false refusal costs
    the caller that edit; unrelated namespaces keep independent admission.
    """
    from . import access, context_refs, find_corpus, state_paths, vault
    from .governance import connector_boundary, membership, policy
    from .governance.principal import effective_principal
    from .memory_refs import ID_FIELD, memory_ref, normalize_id

    root = Path(vault_root).resolve()
    who = effective_principal()
    subjects = []
    identity = None
    for source in (current_source, proposed_source):
        page = find_corpus.parse_page(root / primary_path, 0, root, content=source.encode("utf-8"))
        found = normalize_id(page.frontmatter.get(ID_FIELD)) if page is not None else None
        if page is None or require_identity and (found is None or identity is not None and found != identity):
            raise ValueError("WRITE_REFUSED: target is unavailable")
        identity = found
        classes = page.frontmatter.get("classes") or []
        subjects.append(membership.MetadataSubject(
            path=primary_path, refs=(memory_ref(found) if found else context_refs.vault_ref(primary_path),),
            projects=tuple(value.lower() for value in find_corpus.all_projects(page.frontmatter)),
            tags=tuple(page.tags), types=(page.page_type.lower(),) if page.page_type is not None else (),
            classes=tuple(str(value).lower() for value in classes) if isinstance(classes, list) else (),
        ))
    proposed_hash = _hash(proposed_source)

    def check_admission(path: str) -> bool:
        if effective_principal() != who:
            raise ValueError("WRITE_REFUSED: target is unavailable")
        # Current and proposed admission must precede a content-staleness result.
        if access.writable_reason(root, primary_path) is not None:
            raise ValueError("WRITE_REFUSED: target is unavailable")
        connector_boundary.require_write(root, primary_path, content=proposed_source.encode("utf-8"))
        compiled = policy.load(root)
        boundary = connector_boundary.snapshot(root, compiled)
        denied = boundary.denied_scopes(who) if boundary is not None else frozenset()
        if any(denied.intersection(membership.evaluate_metadata(subject, compiled)) for subject in subjects):
            raise ValueError("WRITE_REFUSED: target is unavailable")
        current, _ = vault.read_guarded_text(root, root / primary_path)
        current_hash = _hash(current)
        if current_hash == primary_guard.expected_content_hash:
            primary_guard.recheck(root)
        elif current_hash != proposed_hash:
            primary_guard.recheck(root)
            raise ValueError("WRITE_REFUSED: target is unavailable")
        return (
            not access.refuse_if_excluded(root, path)
            and all(not denied.intersection(membership.evaluate_metadata(replace(subject, path=path), compiled))
                    for subject in subjects)
        )

    def admit(path: str) -> bool:
        # One invocation reuses host resolution; source and admission checks stay live.
        with state_paths.resolution_scope():
            return check_admission(path)

    return admit


def lifecycle_admission(
    vault_root: Path, *, primary_path: str, current_source: str, proposed_source: str,
    primary_guard: Any,
) -> Callable[[str], bool]:
    """Keep lifecycle admission inside the primary's exact UUID directory."""
    from . import memory_refs, relation_review

    root = Path(vault_root).resolve()
    primary = primary_admission(
        root, primary_path=primary_path, current_source=current_source,
        proposed_source=proposed_source, primary_guard=primary_guard, require_identity=True,
    )
    identity = memory_refs.parse_memory_ref(memory_refs.ref_from_markdown(current_source))
    directory = relation_review.lifecycle_prepared_path(root, identity).parent.relative_to(root).as_posix()

    def admit(path: str) -> bool:
        allowed = primary(path)
        return allowed and path.startswith(f"{directory}/") and "/" not in path.removeprefix(f"{directory}/")

    # Missing or empty directories cannot invoke the census predicate.
    if not admit(f"{directory}/prepared.json"):
        raise ValueError("WRITE_REFUSED: target is unavailable")
    return admit
