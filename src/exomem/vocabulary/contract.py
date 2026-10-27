"""The `schema_memory` contract every vocabulary registry shares.

`inspect`, `propose`, `save`, `history` and `restore` over one `RegistrySpec`.
`commands.op_schema_memory` routes a registry subject here; the old operation
names keep their own handlers.

Mutation authority uses the bound owner principal and the activated v2 writer.
Disclosure admission runs separately before any private registry bytes are read.
A resolved nonowner may queue an admitted, validated proposal for the owner.
An unresolved caller cannot mutate or queue a proposal.

Every registry write answers with `vocabulary_receipt`: one line per key it
registered or changed, naming the registry, the key, the parent and the
restore that reverts it. The line belongs to that write's response only, so it
appears once and asks for nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import instances, registry
from .registry import Entry, RegistryError, RegistrySpec, Snapshot

if TYPE_CHECKING:
    from ..governance.membership import MembershipOutcome
    from ..governance.principal import RequestPrincipal
    from ..vault import PathGuard

INSPECT_DEFAULT_LIMIT = 20
INSPECT_MAX_LIMIT = 200
#: The longest delta a restricted principal's pending proposal can carry.
PENDING_MAX_CHARS = 2000
#: Near-duplicates are keys within this edit distance of a proposed key or alias.
NEAR_DUPLICATE_DISTANCE = 2
# Continuations use the fixed v1 protocol: a digest prefix and a bounded page offset.
_CONTINUATION_RE = re.compile(r"^v1:([0-9a-f]{16}):(\d{1,6})$")


def queues_for_owner(vault_root: Path) -> str | None:
    """Why a save should be queued for the owner instead of written, or None.

    A vault that activated v2 additive authority decided delegation already:
    its writer gate admits a save only under an exact approval or a current
    grant, so a restricted save goes to that gate rather than to the queue.
    """
    from ..governance.principal import effective_principal
    from ..governance.raw_protection import is_owner
    from ..vocabulary_authority import VocabularyAuthority

    who = effective_principal()
    if not who.resolved:
        raise RegistryError("UNRESOLVED_PRINCIPAL: registry mutation requires a bound principal")
    if is_owner(who):
        return None
    try:
        mode = VocabularyAuthority(Path(vault_root)).runtime_status().mode
    except Exception:  # noqa: BLE001 — unreadable authority: the writer gate refuses
        return None
    return "audience_restricted" if mode == "v1" else None


def admission_refusal(vault_root: Path, spec: RegistrySpec) -> dict[str, Any] | None:
    """Admit the adapter's complete overlay before hashes, aliases or entries are read.

    Each S1 adapter reads only its declared overlay and shipped packs. The two
    taxonomy subjects share one overlay, so admission covers both axes together.
    A withheld overlay makes the operation unavailable until domain-aware delivery.
    """
    from ..governance import egress

    return _admission_refusal(
        vault_root, spec,
        release_level=lambda path: egress.release_level_for_path_only(vault_root, path),
        history_storage_level=lambda: _history_storage_level(vault_root, spec),
    )


def _recheck_verified_session(root: Path, who: RequestPrincipal) -> None:
    from ..governance import authorization_custody, authorization_session_lifecycle, store

    context = who.verified_authorization_session
    if context is None:
        return
    if (not who.resolved or who.audience_id != context.principal_id
            or who.authorization_session_id != context.session_id
            or who.issuer_family != context.issuer_family):
        raise RegistryError("REGISTRY_UNAVAILABLE: publication session principal changed")
    # Owner and open-policy shortcuts must still prove current session custody.
    now = int(time.time())
    custody = authorization_custody.load_authorization_custody(root, now=now)
    connection = store.open_authorization_session_connection(root)
    try:
        authorization_session_lifecycle.status_verified_session(
            connection, custody=custody, context=context, now=now,
        )
    finally:
        connection.close()


def _history_storage_level(
    vault_root: Path, spec: RegistrySpec, *, principal: RequestPrincipal | None = None,
    purpose: str | None = None, policy: Any | None = None,
) -> tuple[int, bool]:
    """Return metadata admission and proven absence for the declared history directory."""
    import sqlite3

    from .. import registry_history, reserved_paths, vault
    from ..governance import authorization_custody, authorization_session_lifecycle, egress, store
    from ..governance.principal import effective_principal

    root = Path(vault_root)
    who = principal if principal is not None else effective_principal()
    directory = registry_history.history_dir(root, spec.stem).relative_to(root).as_posix()
    absent = False
    try:
        _recheck_verified_session(root, who)
        try:
            identity = reserved_paths.inspect_generic_path(root, directory)
        except reserved_paths.ReservedPathLeafError as error:
            if error.code != "MISSING":
                return egress.LEVEL_NONE, False
            vault.PathGuard.capture(root, directory, leaf_policy="absent").recheck(root)
            absent = True
        else:
            if identity.kind != "directory":
                return egress.LEVEL_NONE, False
    except (
        RegistryError, vault.PathGuardError, authorization_custody.AuthorizationCustodyUnavailable,
        authorization_session_lifecycle.AuthorizationSessionUnavailable,
        OSError, sqlite3.Error, store.UnsupportedGovernanceSchema,
    ):
        return egress.LEVEL_NONE, False
    # Directory metadata has no content fingerprint; each history file retains byte admission.
    level = egress._release_level_for_path_only_snapshot(
        root, directory, principal=who, purpose=purpose, policy=policy, content_fingerprint=None,
    )
    return level, absent


def _admission_refusal(
    vault_root: Path, spec: RegistrySpec, *, release_level: Callable[[str], int],
    history_storage_level: Callable[[], tuple[int, bool]],
) -> dict[str, Any] | None:
    """Keep instance admission branches shared by readers and publication witnesses."""
    from ..governance.egress import LEVEL_FULL, unrestricted_content_access
    from ..governance.principal import effective_principal
    from ..governance.raw_protection import is_owner

    root = Path(vault_root)
    who = effective_principal()
    if spec.binding_revision is not None:
        document = instances.configuration(root)
        if document is None or instances.revision(document) != spec.binding_revision:
            return {"subject": spec.name, "available": False, "reason": "registry_binding_changed"}
        # Admit both storage owners before any definition, digest or history read.
        path = spec.overlay(root).relative_to(root).as_posix()
        if release_level(path) < LEVEL_FULL or history_storage_level()[0] < LEVEL_FULL:
            return {"subject": spec.name, "available": False, "reason": "audience_restricted"}
        return None
    if is_owner(who) and unrestricted_content_access(root, who):
        return None
    if instances.unassigned_pack_only(root, spec):
        # No overlay or history exists and none can be written unassigned, so
        # the shipped pack is the whole public instance: nothing is withheld.
        return None
    path = spec.overlay(root).relative_to(root).as_posix()
    if release_level(path) < LEVEL_FULL:
        return {"subject": spec.name, "available": False, "reason": "audience_restricted"}
    return None


@dataclass(frozen=True)
class RegistryAdmission:
    """Revalidate one admitted preimage during its synchronous publication only."""

    root: Path
    spec: RegistrySpec
    principal: RequestPrincipal
    purpose: str | None
    policy_fingerprint: str
    before_sha256: str | None
    memberships: tuple[MembershipOutcome, ...]
    requirement_guard: PathGuard

    def _recheck_requirement(self) -> None:
        from .. import vault

        guard = self.requirement_guard
        try:
            if guard.missing_parents:
                # The writer pins new parents; retain old ancestry and prove the requirement still absent.
                guard.recheck_ancestors(self.root)
                vault.PathGuard.capture(self.root, guard.target, leaf_policy="absent").recheck(self.root)
            else:
                guard.recheck(self.root)
        except vault.PathGuardError as error:
            raise RegistryError("REGISTRY_UNAVAILABLE: selected instance changed or is unavailable") from error

    def recheck(self) -> None:
        from .. import registry_history
        from ..governance import egress, membership, policy
        from ..governance.principal import effective_principal

        root = self.root
        who = effective_principal()
        self._recheck_requirement()
        instances.recheck_binding(root, self.spec)
        compiled = policy.load(root)
        if (who != self.principal or compiled.fingerprint != self.policy_fingerprint
                or egress._declared_purpose(root, who, None) != self.purpose):
            raise RegistryError("REGISTRY_UNAVAILABLE: publication admission changed")
        _recheck_verified_session(root, who)
        paths = (self.spec.overlay(root).relative_to(root).as_posix(),)
        if self.spec.binding_revision is not None:
            paths += (registry_history.history_dir(root, self.spec.stem).relative_to(root).as_posix(),)
        current_memberships = tuple(
            membership.evaluate_path_only(root, path, compiled) for path in paths
        )
        # Creating missing parents can change a refusal reason without changing its authority.
        if any(current.state != before.state or current.scope_ids != before.scope_ids
               for current, before in zip(current_memberships, self.memberships, strict=True)):
            raise RegistryError("REGISTRY_UNAVAILABLE: publication membership changed")
        refusal = _admission_refusal(
            root, self.spec,
            release_level=lambda path: egress._release_level_for_path_only_snapshot(
                root, path, principal=who, purpose=self.purpose, policy=compiled,
                content_fingerprint=self.before_sha256,
            ),
            history_storage_level=lambda: _history_storage_level(
                root, self.spec, principal=who, purpose=self.purpose, policy=compiled,
            ),
        )
        if refusal is not None:
            raise RegistryError("REGISTRY_UNAVAILABLE: publication admission is unavailable")
        self._recheck_requirement()
        if policy.load(root).fingerprint != self.policy_fingerprint:
            raise RegistryError("REGISTRY_UNAVAILABLE: publication policy changed")


def capture_admission(root: Path, spec: RegistrySpec, before: bytes | None) -> RegistryAdmission:
    """Prove ordinary admission and the exact preimage before retaining a witness."""
    from .. import registry_history, vault
    from ..governance import connector_boundary, egress, membership, policy
    from ..governance.principal import effective_principal

    root = Path(root)
    requirement_path = connector_boundary.requirement_relative_path()
    try:
        (root / requirement_path).lstat()
    except FileNotFoundError:
        requirement = None
        requirement_guard = vault.PathGuard.capture(root, requirement_path, leaf_policy="absent")
    else:
        raw, requirement_guard = vault.read_bounded_guarded_bytes(
            root, requirement_path, limit=connector_boundary._MAX_CONFIG_BYTES,
        )
        requirement = connector_boundary.parse_requirement(raw)
    if requirement != connector_boundary.read_requirement(root):
        raise RegistryError("REGISTRY_UNAVAILABLE: publication binding changed")
    instances.recheck_binding(root, spec)
    who = effective_principal()
    compiled = policy.load(root)
    purpose = egress._declared_purpose(root, who, None)
    refusal = _admission_refusal(
        root, spec,
        release_level=lambda path: egress.release_level_for_path_only(
            root, path, principal=who, purpose=purpose, policy=compiled,
        ),
        history_storage_level=lambda: _history_storage_level(
            root, spec, principal=who, purpose=purpose, policy=compiled,
        ),
    )
    if refusal is not None:
        raise RegistryError("REGISTRY_UNAVAILABLE: publication admission is unavailable")
    path = spec.overlay(root).relative_to(root).as_posix()
    try:
        (root / path).lstat()
    except FileNotFoundError:
        current = None
        preimage_guard = vault.PathGuard.capture(root, path, leaf_policy="absent")
    else:
        current, preimage_guard = vault.read_bounded_guarded_bytes(root, path, limit=registry.MAX_OVERLAY_BYTES)
    if current != before:
        raise vault.PathGuardError("PATH_GUARD_CONTENT", "publication preimage changed")
    paths = (path,)
    if spec.binding_revision is not None:
        paths += (registry_history.history_dir(root, spec.stem).relative_to(root).as_posix(),)
    witness = RegistryAdmission(
        root, spec, who, purpose, compiled.fingerprint,
        hashlib.sha256(before).hexdigest() if before is not None else None,
        tuple(membership.evaluate_path_only(root, item, compiled) for item in paths), requirement_guard,
    )
    witness.recheck()
    preimage_guard.recheck(root)
    return witness


def selection_refusal(subject: str, error: RegistryError) -> dict[str, Any]:
    """The refusal for an instance that cannot be selected; it names no definition."""
    reason = ("registry_assignment_required" if isinstance(error, instances.AssignmentRequired)
              else "audience_restricted")
    return {"subject": subject, "available": False, "reason": reason}


def selected_admission_refusal(vault_root: Path, spec: RegistrySpec) -> dict[str, Any] | None:
    """Admit the public instance that a read of `spec` selects, the one `registry.load` reads.

    A selection or binding error makes the registry unavailable instead of
    escaping the caller.
    """
    if not Path(vault_root).is_dir():
        return None  # A vault that does not exist holds no overlay to withhold; reads get the pack.
    try:
        spec = instances.select(Path(vault_root), spec)
    except RegistryError as error:
        return selection_refusal(spec.name, error)
    return admission_refusal(vault_root, spec)


def history_refusal(vault_root: Path, spec: RegistrySpec) -> dict[str, Any] | None:
    """Admit every kept snapshot before a history producer reads its header or bytes."""
    from .. import registry_history
    from ..governance import egress

    root = Path(vault_root)
    directory = registry_history.history_dir(root, spec.stem).relative_to(root).as_posix()
    try:
        root.lstat()
    except FileNotFoundError:
        # No vault holds history; later files still require consumed-byte admission.
        return None
    except OSError:
        return {"subject": spec.name, "available": False, "reason": "audience_restricted"}
    level, absent = _history_storage_level(root, spec)
    if level < egress.LEVEL_FULL:
        return {"subject": spec.name, "available": False, "reason": "audience_restricted"}
    if absent:
        return None
    names = registry_history.kept_names(root, directory, suffix=".yaml")
    if names is None or any(
        egress.release_level_for_path_only(root, f"{directory}/{name}") < egress.LEVEL_FULL
        for name in names[: registry_history.HISTORY_KEEP]
    ):
        return {"subject": spec.name, "available": False, "reason": "audience_restricted"}
    return None


def _usage(
    vault_root: Path, spec: RegistrySpec, snapshot: Snapshot, *, for_removal: bool = False,
) -> Any:
    from .usage import Usage

    if spec.binding_revision is not None:
        # Existing aggregate projections have no instance identity; never union their counts.
        return Usage(available=False, reason="instance_projection_unavailable")
    read = spec.removal_usage if for_removal and spec.removal_usage is not None else spec.usage
    if read is None:
        return Usage(available=False, reason="not_counted")
    return read(vault_root, snapshot)


def _ordered(snapshot: Snapshot, usage: Any) -> list[Entry]:
    entries = list(snapshot.entries.values())
    if usage.available:
        return sorted(
            entries,
            key=lambda entry: (
                entry.status != "active",
                -usage.counts.get(entry.key, 0),
                entry.key,
            ),
        )
    return entries


def save_contract(spec: RegistrySpec) -> dict[str, Any]:
    """What a `save` delta for this registry may carry, for a caller to read."""
    return {
        # `remove` is offered where a usage check can show a key unused first.
        "verbs": ["upsert", "alias", "deprecate", *(["remove"] if spec.usage is not None else [])],
        "fields": sorted(spec.fields),
        "attributes": sorted(spec.attributes),
        "fixed_once_saved": sorted(spec.immutable),
        "replacement_required": spec.replacement_required,
        "promotion": spec.promotion,
    }


def inspect(
    vault_root: Path,
    spec: RegistrySpec,
    *,
    limit: int | None = None,
    continuation: str | None = None,
) -> dict[str, Any]:
    """The live registry in the generic entry shape, paginated, with usage counts."""
    if spec.binding_revision is None:
        spec = instances.select(vault_root, spec, authoring=True)
    size = INSPECT_DEFAULT_LIMIT if limit is None else int(limit)
    if not 1 <= size <= INSPECT_MAX_LIMIT:
        raise RegistryError(f"INVALID_REGISTRY_ARGUMENT: limit must be 1 to {INSPECT_MAX_LIMIT}")
    refusal = admission_refusal(vault_root, spec)
    if refusal is not None:
        return refusal
    snapshot = registry.load(spec, vault_root)
    offset = 0
    if continuation is not None:
        match = _CONTINUATION_RE.fullmatch(str(continuation))
        if match is None or match.group(1) != snapshot.effective_digest[:16]:
            raise RegistryError(
                "STALE_REGISTRY_CONTINUATION: the registry changed; inspect again from the start"
            )
        offset = int(match.group(2))
    usage = _usage(vault_root, spec, snapshot)
    ordered = _ordered(snapshot, usage)
    page = ordered[offset : offset + size]
    entries = []
    for entry in page:
        row = entry.as_dict()
        if usage.available:
            row["count"] = usage.counts.get(entry.key, 0)
        entries.append(row)
    following = offset + len(page)
    out: dict[str, Any] = {
        "subject": spec.name,
        "content_hash": snapshot.content_hash,
        "effective_digest": snapshot.effective_digest,
        "total": len(ordered),
        "returned": len(page),
        "counts": "available" if usage.available else "unavailable",
        "entries": entries,
        "findings": [dict(item) for item in snapshot.findings],
        "save": save_contract(spec),
        "continuation": (
            f"v1:{snapshot.effective_digest[:16]}:{following}" if following < len(ordered) else None
        ),
    }
    if usage.available:
        out["count_source"] = usage.source
    else:
        out["reason"] = usage.reason
    extra = getattr(spec.adapter, "inspect_extra", None)
    if extra is not None:
        out.update(extra(vault_root))
    return out


def _tokens(entry: Entry) -> dict[str, str]:
    """Each comparison token an entry claims, with the field that claims it."""
    tokens: dict[str, str] = {registry.token(entry.key): "key"}
    if entry.label:
        tokens.setdefault(registry.token(entry.label), "label")
    for alias in entry.aliases:
        tokens.setdefault(registry.token(alias), "aliases")
    for name in ("folder", "path_label"):
        value = entry.attributes.get(name)
        if isinstance(value, str) and value:
            tokens.setdefault(registry.token(value), f"attributes.{name}")
    tokens.pop("", None)
    return tokens


def _collisions(snapshot: Snapshot, proposed: Entry) -> list[dict[str, str]]:
    owners: dict[str, tuple[str, str]] = {}
    for entry in snapshot.entries.values():
        if entry.key == proposed.key:
            continue
        for value, field_name in _tokens(entry).items():
            owners.setdefault(value, (entry.key, field_name))
    out = []
    for value, field_name in _tokens(proposed).items():
        owner = owners.get(value)
        if owner is not None:
            out.append(
                {"key": owner[0], "field": field_name, "value": value, "existing_field": owner[1]}
            )
    return out


def _near_duplicates(
    snapshot: Snapshot, proposed: Entry, usage: Any, exact: set[str]
) -> list[dict[str, Any]]:
    from ..project_keys import _levenshtein

    mine = set(_tokens(proposed))
    out: list[dict[str, Any]] = []
    for entry in snapshot.entries.values():
        if entry.key == proposed.key or entry.key in exact:
            continue
        theirs = set(_tokens(entry))
        distance = min(
            (
                _levenshtein(left, right, max_dist=NEAR_DUPLICATE_DISTANCE)
                for left in mine
                for right in theirs
            ),
            default=NEAR_DUPLICATE_DISTANCE + 1,
        )
        if distance <= NEAR_DUPLICATE_DISTANCE:
            row: dict[str, Any] = {"key": entry.key, "distance": distance, "status": entry.status}
            if usage.available:
                row["count"] = usage.counts.get(entry.key, 0)
            out.append(row)
    return sorted(out, key=lambda row: (row["distance"], row["key"]))


def _inherited(spec: RegistrySpec, snapshot: Snapshot, entry: Entry) -> dict[str, Any]:
    """Attributes the entry takes from its parent because it does not set them."""
    parent = snapshot.entries.get(entry.parent or "")
    if parent is None:
        return {}
    return {
        name: registry._plain(value)
        for name, value in parent.attributes.items()
        if name in spec.attributes and name not in entry.attributes
    }


#: Most pages a removal report lists for one still-used key.
DEPENDANT_PAGES = 20
#: Pages one receipt line names; the full report carries `DEPENDANT_PAGES`.
_RECEIPT_PAGES = 3


def _removal_report(
    after_entries: Mapping[str, Entry], delta: object, usage: Any
) -> dict[str, dict[str, Any]]:
    """What a `remove` delta did to each key it named: removed, or deprecated with its pages."""
    names = delta.get("remove") if isinstance(delta, Mapping) else None
    report: dict[str, dict[str, Any]] = {}
    for key in names or ():
        entry = after_entries.get(key)
        if entry is None:
            report[key] = {"action": "removed"}
            continue
        pages = usage.holders(key, DEPENDANT_PAGES) if usage.holders is not None else None
        report[key] = {
            "action": "deprecated",
            "replaced_by": entry.replaced_by,
            "dependants": {
                "count": usage.count(key),
                "source": usage.source,
                "pages": None if pages is None else list(pages),
            },
        }
    return report


def _candidate(
    vault_root: Path, spec: RegistrySpec, snapshot: Snapshot, delta: object
) -> tuple[dict[str, Any], dict[str, Entry], Any, list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """The overlay document after a delta, its typed registry, blocking findings and removals."""
    removing = isinstance(delta, Mapping) and bool(delta.get("remove"))
    usage = _usage(vault_root, spec, snapshot, for_removal=True) if removing else None
    document, touched = registry.apply_delta(spec, snapshot, delta, usage=usage)
    candidate = spec.adapter.parse_document(document)
    after_entries = spec.adapter.entries(candidate)
    registry.validate_replacements(spec, after_entries)
    blocking = registry.new_findings(snapshot, tuple(spec.adapter.findings(candidate)))
    removal = _removal_report(after_entries, delta, usage) if removing else {}
    continuity = getattr(spec.adapter, "continuity", None)
    if continuity is not None:
        # A key the delta removed after counting it unused is no continuity break.
        gone = {key for key, outcome in removal.items() if outcome["action"] == "removed"}
        blocking.extend(dict(item) for item in continuity(snapshot.typed, candidate, gone))
    admission = getattr(spec.adapter, "admission", None)
    if admission is not None:
        # Checks that read the vault run for the entries this delta touched, so state that moved
        # since an earlier save never blocks an unrelated one.
        blocking.extend(
            dict(item) for item in admission(vault_root, candidate, {k: v for k, v in touched.items() if k not in removal})
        )
    vault_entries = sum(1 for entry in after_entries.values() if entry.origin == "vault")
    if vault_entries > spec.cap:
        blocking.append(
            {
                "code": "registry_cap",
                "path": spec.name,
                "severity": "error",
                "detail": f"{spec.name} holds at most {spec.cap} vault entries",
            }
        )
    return document, touched, candidate, blocking, removal


def propose(vault_root: Path, spec: RegistrySpec, delta: object) -> dict[str, Any]:
    """Read-only: what a save of `delta` would register, and what it resembles."""
    if spec.binding_revision is None:
        spec = instances.select(vault_root, spec, authoring=True)
    refusal = admission_refusal(vault_root, spec)
    if refusal is not None:
        return refusal
    snapshot = registry.load(spec, vault_root)
    out: dict[str, Any] = {"subject": spec.name, "expected_hash": snapshot.content_hash}
    try:
        _document, touched, candidate, blocking, removal = _candidate(
            vault_root, spec, snapshot, delta
        )
    except RegistryError as exc:
        code, _, detail = str(exc).partition(": ")
        return {
            **out,
            "valid": False,
            "entries": [],
            "collisions": [],
            "near_duplicates": [],
            "findings": [{"code": code, "severity": "error", "detail": detail or str(exc)}],
            "save": save_contract(spec),
        }
    usage = _usage(vault_root, spec, snapshot)
    candidate_entries = spec.adapter.entries(candidate)
    rows: list[dict[str, Any]] = []
    collisions: list[dict[str, str]] = []
    near: list[dict[str, Any]] = []
    for key, proposed in touched.items():
        if key in removal:
            rows.append(
                {"key": key, "exists": True, "valid": True, "entry": proposed.as_dict(),
                 "action": removal[key]["action"]}
            )
            continue
        # The validated entry when it survived validation, else the proposal as sent.
        entry = candidate_entries.get(key, proposed)
        found = _collisions(snapshot, entry)
        collisions.extend(found)
        near.extend(_near_duplicates(snapshot, entry, usage, {item["key"] for item in found}))
        rows.append(
            {
                "key": key,
                "exists": key in snapshot.entries,
                "valid": key in candidate_entries,
                "entry": entry.as_dict(),
                "inherited": _inherited(spec, snapshot, entry),
            }
        )
    return {
        **out,
        "valid": not blocking,
        "entries": rows,
        "collisions": collisions,
        "near_duplicates": near,
        "counts": "available" if usage.available else "unavailable",
        "findings": blocking,
        "save": save_contract(spec),
        **({"removal": removal} if removal else {}),
    }


def _restore_route(spec: RegistrySpec, version: str) -> str:
    selector = (f', registry_scope="{spec.instance_id}"'
                if spec.instance_id != registry.PUBLIC_INSTANCE else "")
    return (
        f'schema_memory(subject="{spec.name}", operation="restore", version="{version}", '
        f"expected_hash, why{selector})"
    )


def _removal_line(spec: RegistrySpec, key: str, outcome: Mapping[str, Any], route: str) -> str:
    """The receipt line for a removed or deprecated key; a pages list that is too long drops."""
    if outcome["action"] == "removed":
        return f"{spec.name}: removed {key}, which nothing used. Revert: {route}"
    dependants = outcome["dependants"]
    pages = dependants["pages"] or []
    successor = f" in favour of {outcome['replaced_by']}" if outcome["replaced_by"] else ""
    shown = ", ".join(pages[:_RECEIPT_PAGES])
    more = f" and {len(pages) - _RECEIPT_PAGES} more" if len(pages) > _RECEIPT_PAGES else ""
    uses = f"; {dependants['count']} {dependants['source']} still use it"
    line = f"{spec.name}: deprecated {key}{successor}{uses}"
    full = f"{line}: {shown}{more}. Revert: {route}" if shown else f"{line}. Revert: {route}"
    from .. import mutation_terminal

    # A receipt line past the terminal's bound would drop the whole receipt.
    bound = mutation_terminal._VOCABULARY_RECEIPT_CHARS
    return full if len(full) <= bound else f"{line}. Revert: {route}"


def receipt_lines(
    spec: RegistrySpec,
    before: Snapshot,
    after_entries: Mapping[str, Entry],
    touched: tuple[str, ...],
    version: str,
    removal: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """One line per key a write registered or changed, with its revert route."""
    lines: list[str] = []
    for key in touched:
        if removal and key in removal:
            lines.append(_removal_line(spec, key, removal[key], _restore_route(spec, version)))
            continue
        entry = after_entries.get(key)
        if entry is None:
            continue
        previous = before.entries.get(key)
        if previous is None:
            verb = "registered"
        elif entry.status == "deprecated" and previous.status != "deprecated":
            verb = (
                f"deprecated in favour of {entry.replaced_by}"
                if entry.replaced_by
                else "deprecated"
            )
        else:
            verb = "changed"
        parent = f"parent {entry.parent}" if entry.parent else "no parent"
        lines.append(
            f"{spec.name}: {verb} {key} ({parent}); in effect now for every agent. "
            f"Revert: {_restore_route(spec, version)}"
        )
    return lines


def first_use_lines(added: Mapping[str, Sequence[str]], version: str) -> list[str]:
    """One line per key a write registered on first use, with its revert route.

    An open registry (source kinds, domains) registers a key the first time a
    write uses it. The line is the write's own receipt: it appears once, in
    that write's response, and asks for nothing.
    """
    from . import registry_spec

    return [
        f"{name}: registered {key} on first use; in effect now for every agent. "
        f"Revert: {_restore_route(registry_spec(name), version)}"
        for name, keys in added.items()
        for key in keys
    ]


def queue_for_owner(
    vault_root: Path,
    spec: RegistrySpec,
    snapshot: Snapshot,
    proposal: object,
    why: str,
    reason: str,
    *,
    operation: str = "save",
) -> dict[str, Any]:
    """Queue a restricted principal's save for the owner; change nothing.

    The pending item carries the operation, the reason and the proposal as the
    caller sent them, so the owner reviews exactly what was asked.
    """
    if spec.family is None:
        return {"subject": spec.name, "available": False, "reason": reason, "saved": None}
    from .. import vocabulary_review
    from ..vocabulary_state import VocabularyState
    from ..vocabulary_workflow import Evidence, make_item

    question = json.dumps(
        {"registry": spec.name, "operation": operation, "why": why, "proposal": proposal},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(question) > PENDING_MAX_CHARS:
        raise RegistryError(
            f"PENDING_PROPOSAL_TOO_LARGE: a proposal for the owner holds at most "
            f"{PENDING_MAX_CHARS} characters; save one entry at a time"
        )
    root = Path(vault_root)
    if spec.binding_revision is None:
        spec = instances.select(root, spec, authoring=True)
    target = spec.overlay(root).relative_to(root).as_posix()
    item = make_item(
        family=spec.family,
        signal=vocabulary_review.REGISTRY_PROPOSAL_SIGNAL,
        targets={target: snapshot.content_hash},
        evidence=[
            Evidence(target, snapshot.content_hash, vocabulary_review.REGISTRY_PROPOSAL_SIGNAL)
        ],
        # Validation reads only this target overlay; its server hash binds currency.
        registry_hashes={},
        projection_status="current",
        paths={target: target},
        question=question,
        logical_identity=f"registry-proposal:{spec.name}:{registry.content_hash(target + ':' + question)[:32]}",
    )
    VocabularyState(root).observe(item)
    return {
        "subject": spec.name,
        "state": "pending_review",
        "item_ref": item.ref,
        "reason": reason,
        "saved": None,
        "vocabulary_receipt": [
            f"{spec.name}: proposal queued for the owner as {item.ref}; the registry is "
            "unchanged. Write with the raw label meanwhile."
        ],
    }


def _promotion_removal(root: Path, spec: RegistrySpec, delta: object) -> tuple[object, tuple[Any, ...]]:
    from .. import promotion_proof, promotions

    if not isinstance(delta, Mapping) or "_promotion" not in delta:
        return delta, ()
    binding = promotion_proof.metadata(delta["_promotion"])
    # nosemgrep: ep-word-set, ep-word-membership -- the reserved undo protocol fixes this removal shape.
    if set(delta) != {"remove", "_promotion"} or not isinstance(delta["remove"], list):
        raise RegistryError("PROMOTION_SUBJECT_UNVERIFIED: undo requires a removal-only proposal")
    _promotion_id, entry = promotions.find(root, binding["ref"])
    if entry["kind"] != "registry" or any(
        item.get("registry") != spec.name
        or (item.get("scope") or registry.PUBLIC_INSTANCE) != spec.instance_id
        for item in entry["subjects"]
    ):
        raise RegistryError("PROMOTION_SUBJECT_UNVERIFIED: registry undo binding does not match")
    proofs = [promotion_proof.registry(root, item, spec=spec) for item in entry["subjects"]]
    if any(proof.status == "unverified" for proof in proofs):
        raise RegistryError("PROMOTION_SUBJECT_UNVERIFIED: original registry continuity is unavailable")
    keys = [item["key"] for item, proof in zip(entry["subjects"], proofs, strict=True) if proof.status == "same"]
    if delta["remove"] != keys or not keys or binding["expected_fingerprint"] != promotion_proof.fingerprint(proofs):
        raise RegistryError("PROMOTION_SUBJECT_CHANGED: original registry subjects changed")
    return {"remove": keys}, tuple(guard for proof in proofs for guard in proof.guards)


def save(
    vault_root: Path,
    spec: RegistrySpec,
    *,
    delta: object,
    expected_hash: str | None,
    why: str | None,
) -> dict[str, Any]:
    """Apply one hash-guarded delta with its reason, keeping history."""
    if not isinstance(why, str) or not why.strip():
        raise RegistryError("WHY_REQUIRED: save requires why")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise RegistryError(
            "EXPECTED_HASH_REQUIRED: save requires expected_hash from inspect or propose"
        )
    root = Path(vault_root)
    reason = queues_for_owner(root)
    if spec.binding_revision is None:
        spec = instances.select(root, spec, authoring=True)
    refusal = admission_refusal(root, spec)
    if refusal is not None:
        return refusal
    snapshot = registry.load(spec, root)
    if expected_hash != snapshot.content_hash:
        raise RegistryError(
            f"STALE_REGISTRY: {spec.name} changed since expected_hash was read; inspect again"
        )
    delta, proof_guards = _promotion_removal(root, spec, delta)
    document, touched, candidate, blocking, removal = _candidate(root, spec, snapshot, delta)
    if blocking:
        return {"subject": spec.name, "valid": False, "findings": blocking, "saved": None}
    if reason is not None:
        # Validated like the owner's save, so the owner reviews only deltas that apply.
        return queue_for_owner(root, spec, snapshot, delta, why.strip(), reason)
    rendered = spec.adapter.render(document)
    if len(rendered.encode("utf-8")) > registry.MAX_OVERLAY_BYTES:
        raise RegistryError(
            f"REGISTRY_TOO_LARGE: {spec.name} overlay would exceed "
            f"{registry.MAX_OVERLAY_BYTES} bytes"
        )
    after_entries = spec.adapter.entries(candidate)
    added = registry.added_keys(snapshot.entries, after_entries)
    history = registry.commit(
        spec,
        root,
        rendered,
        operation="save",
        why=why.strip(),
        before_hash=snapshot.content_hash,
        previous=snapshot.overlay_text,
        added=added,
        required_guards=proof_guards,
    )
    _mark_committed()
    after = registry._build(spec, rendered)
    return {
        "subject": spec.name,
        "valid": True,
        "findings": [],
        "why": why.strip(),
        "changed_keys": list(touched),
        **({"removal": removal} if removal else {}),
        "vocabulary_receipt": receipt_lines(
            spec, snapshot, after.entries, tuple(touched), history["version"], removal
        ),
        "saved": {
            "path": spec.overlay(root).relative_to(root).as_posix(),
            "content_hash": after.content_hash,
            "previous_hash": snapshot.content_hash,
            "effective_digest": after.effective_digest,
            "history": history,
        },
    }


def history(vault_root: Path, spec: RegistrySpec) -> dict[str, Any]:
    """The kept versions, newest first; reasons and principals for the owner only."""
    from .. import registry_history

    root = Path(vault_root)
    if spec.binding_revision is None:
        spec = instances.select(root, spec, authoring=True)
    refusal = admission_refusal(root, spec) or history_refusal(root, spec)
    if refusal is not None:
        return refusal
    snapshot = registry.load(spec, root)
    try:
        view = registry_history.history_view(
            root, stem=spec.stem, instance_admitted=spec.binding_revision is not None,
        )
    except registry_history.HistoryAdmissionRefused:
        return {"subject": spec.name, "available": False, "reason": "audience_restricted"}
    return {
        "subject": spec.name,
        "content_hash": snapshot.content_hash,
        **view,
    }


def restore(
    vault_root: Path,
    spec: RegistrySpec,
    *,
    version: str | None,
    expected_hash: str | None,
    why: str | None,
) -> dict[str, Any]:
    """Write a kept version's exact bytes back as a governed save of its own.

    Reverting is the point, so the in-place meaning rules do not apply; pages
    are never touched, and a page that used a removed key becomes debt.
    """
    from .. import registry_history

    if not version:
        raise RegistryError("INVALID_REGISTRY_ARGUMENT: restore requires version from history")
    if not isinstance(why, str) or not why.strip():
        raise RegistryError("WHY_REQUIRED: restore requires why")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise RegistryError("EXPECTED_HASH_REQUIRED: restore requires expected_hash")
    root = Path(vault_root)
    from ..governance.principal import effective_principal
    from ..governance.raw_protection import is_owner

    if not is_owner(effective_principal()):
        return {"subject": spec.name, "available": False, "reason": "audience_restricted"}
    if spec.binding_revision is None:
        spec = instances.select(root, spec, authoring=True)
    refusal = admission_refusal(root, spec) or history_refusal(root, spec)
    if refusal is not None:
        return refusal
    snapshot = registry.load(spec, root)
    if expected_hash != snapshot.content_hash:
        raise RegistryError(
            f"STALE_REGISTRY: {spec.name} changed since expected_hash was read; inspect again"
        )
    try:
        text = registry_history.read_version(root, stem=spec.stem, version=version)
    except registry_history.HistoryAdmissionRefused:
        return {"subject": spec.name, "available": False, "reason": "audience_restricted"}
    restored = spec.adapter.parse(text, registry.content_hash(text))
    # Restore preserves legacy reader meaning, but never republishes invalid bytes.
    blocking = registry.new_findings(None, tuple(spec.adapter.findings(restored)))
    if blocking:
        return {"subject": spec.name, "valid": False, "findings": blocking, "saved": None}
    after_entries = spec.adapter.entries(restored)
    removed = sorted(set(snapshot.entries) - set(after_entries))
    # Taxonomy subjects share one overlay; an exact restore can remove keys on both axes.
    from . import registry_specs

    removed_by_registry = {}
    for related in registry_specs().values():
        if spec.binding_revision is not None:
            related = instances.select(root, related,
                                       None if spec.instance_id == registry.PUBLIC_INSTANCE else spec.instance_id)
        if related.overlay(root) != spec.overlay(root):
            continue
        before = related.adapter.entries(
            related.adapter.parse(
                snapshot.overlay_text,
                snapshot.content_hash,
            )
        )
        after_related = related.adapter.entries(
            related.adapter.parse(text, registry.content_hash(text))
        )
        missing = sorted(set(before) - set(after_related))
        if missing:
            removed_by_registry[related.name] = missing
    added = registry.added_keys(snapshot.entries, after_entries)
    history = registry.commit(
        spec,
        root,
        text,
        operation="restore",
        why=why.strip(),
        before_hash=snapshot.content_hash,
        previous=snapshot.overlay_text,
        added=added,
    )
    _mark_committed()
    after = registry._build(spec, text)
    return {
        "subject": spec.name,
        "valid": True,
        "why": why.strip(),
        "restored": version,
        "removed_keys": removed,
        "removed_by_registry": removed_by_registry,
        "findings": [dict(item) for item in after.findings],
        "vocabulary_receipt": [
            f"{spec.name}: restored version {version}"
            + (f"; removed {', '.join(removed)}" if removed else "")
            + ". Pages keep their bytes; a page using a removed key is now unregistered debt. "
            f"Undo: {_restore_route(spec, history['version'])}"
        ],
        "saved": {
            "path": spec.overlay(root).relative_to(root).as_posix(),
            "content_hash": after.content_hash,
            "previous_hash": snapshot.content_hash,
            "effective_digest": after.effective_digest,
            "history": history,
        },
    }


def _mark_committed() -> None:
    from ..writer_lease import mark_active_mutation_committed

    mark_active_mutation_committed()
