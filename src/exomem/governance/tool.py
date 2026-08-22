"""Deterministic governance-authoring router.

The language model chooses an operation and supplies user intent.  This module
owns every enforcement fact: operation coverage, caller/session authority,
proposal and token bounds, current membership, durable receipts, and recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .. import (
    deferred_index,
    epistemic_graph,
    find_corpus,
    graph_sync,
    index_paths,
    lexstore,
    media_jobs,
    memory_refs,
    reserved_paths,
    review_state,
)
from ..kbdir import kb_dirname
from . import companion_backfill, decisions, membership, receipts, store
from . import policy as policy_module
from . import tokens as tokens_module
from .operations import (
    HANDLER_STRATEGY_KEYS,
    OPERATION_SPECS,
    OperationSpec,
    OperationVariant,
    assert_operation_coverage,
    operation_variant,
    select_operation,
)
from .principal import (
    OWNER_AUDIENCE,
    UNNAMED_AUDIENCE_PROBE,
    RequestPrincipal,
    effective_principal,
)
from .transaction import (
    GovernanceCrash,
    GovernanceError,
    authorization_row,
    policy_target,
)
from .transaction import fsync_directory as _fsync_directory

PENDING_MARKER = ".policy-mutation.pending.json"
DEFAULT_PROPOSAL_TTL_SECONDS = 900
_GRAPH_REBUILD_SIDECAR_RE = re.compile(
    rf"^{re.escape(graph_sync._TEMP_PREFIX)}[0-9a-f]{{64}}-[0-9a-f]{{24}}\.sqlite"
    r"(?:-(?:wal|shm|journal))?$"
)
_REVIEW_STATE_TEMP_RE = re.compile(r"^\.\.review-state\.json\.[a-z0-9_]{8}\.tmp$")
_LEXICAL_REBUILD_TEMP_RE = re.compile(
    r"^\.lexical\.sqlite\.rebuild-[0-9a-f]{32}\.tmp(?:-(?:wal|shm|journal))?$"
)

__all__ = [
    "GovernanceCrash",
    "GovernanceError",
    "OPERATION_SPECS",
    "OperationSpec",
    "assert_operation_coverage",
    "classify_transition_direction",
    "op_govern_memory",
    "reconcile_governance_operations",
]


def _principal(value: RequestPrincipal | None) -> RequestPrincipal:
    return value if value is not None else effective_principal()


def _require_owner(value: RequestPrincipal | None) -> RequestPrincipal:
    who = _principal(value)
    if not who.resolved or who.audience_id != OWNER_AUDIENCE:
        raise GovernanceError("GOVERNANCE_OWNER_REQUIRED", "operation is owner-only")
    return who


def _prospective_policy(
    vault_root: Path, documents: Mapping[str, str | None]
) -> policy_module.Policy:
    prospective = policy_module.compile_prospective(vault_root, dict(documents))
    if prospective is None:
        raise GovernanceError(
            "GOVERNANCE_AUTHORING_UNSTABLE",
            "the policy workspace changed or could not be acquired safely",
        )
    return prospective.policy


def _require_authorization_session(
    value: RequestPrincipal | None, supplied: Any
) -> tuple[RequestPrincipal, str]:
    who = _principal(value)
    handle = str(supplied or "").strip()
    if (
        not who.resolved
        or not handle
        or not who.authorization_session_id
        or not __import__("hmac").compare_digest(handle, who.authorization_session_id)
    ):
        raise GovernanceError(
            "AUTHORIZATION_SESSION_REQUIRED",
            "an explicit authorization session bound to this principal is required",
        )
    return who, handle


def _authorize_operation(selection: OperationVariant, kwargs: Mapping[str, Any]) -> None:
    """Apply the registry's coarse authorization before handler-specific bounds."""
    if selection.authorization == "owner":
        _require_owner(kwargs.get("principal"))
    elif selection.authorization in {"self_session", "token_session"}:
        _require_authorization_session(
            kwargs.get("principal"), kwargs.get("authorization_session")
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _content_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"


def _canonical_documents(documents: Any) -> dict[str, str]:
    if not isinstance(documents, Mapping) or not documents:
        raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "documents must be a mapping")
    canonical: dict[str, str] = {}
    for raw_path, raw_content in sorted(documents.items(), key=lambda item: str(item[0])):
        rel = str(raw_path).replace("\\", "/").strip("/")
        parts = Path(rel).parts
        if (
            Path(rel).is_absolute()
            or not parts
            or parts[0] not in {"scopes", "rules", "grants"}
            or any(part in {"", ".", ".."} for part in parts)
            or not rel.endswith(".yaml")
        ):
            raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "invalid policy path")
        if not isinstance(raw_content, str):
            raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "policy YAML must be text")
        try:
            parsed = yaml.safe_load(raw_content)
        except yaml.YAMLError as exc:
            raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "invalid policy YAML") from exc
        if not isinstance(parsed, dict):
            raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "policy YAML must be a mapping")
        canonical[rel] = raw_content.rstrip() + "\n"
    return canonical


def _affected_scope_ids(
    current: policy_module.Policy,
    prospective: policy_module.Policy,
    document_paths: set[str],
) -> frozenset[str]:
    affected: set[str] = set()
    for candidate in (current, prospective):
        for scope in candidate.scopes.values():
            if scope.source in document_paths:
                affected.add(scope.id)
        for document in (*candidate.rules, *candidate.grants):
            if document.source in document_paths:
                affected.update(document.scope_ids)
    return frozenset(affected)


def _memberships_for_path(
    vault_root: Path, path: Path, candidate: policy_module.Policy
) -> frozenset[str]:
    rel = path.relative_to(vault_root).as_posix()
    if path.suffix.casefold() != ".md":
        try:
            return membership.evaluate_path_only(
                vault_root, rel, candidate
            ).require_classified()
        except membership.MembershipUnresolved as exc:
            raise GovernanceError(
                "MEMBERSHIP_UNRESOLVED", "cannot resolve exact non-Markdown membership"
            ) from exc
    try:
        page = find_corpus.parse_page(path, path.stat().st_mtime, vault_root)
        return membership.evaluate(page, candidate)
    except (OSError, UnicodeError, membership.MembershipUnresolved) as exc:
        raise GovernanceError(
            "MEMBERSHIP_UNRESOLVED", f"cannot resolve exact membership for {rel!r}"
        ) from exc


def _is_operational_membership_path(vault_root: Path, candidate: Path) -> bool:
    """Whether a current internal-state owner, rather than content, owns a path."""
    from .. import claims, voice_profiles

    sidecars = (
        index_paths.sidecar_path(vault_root),
        index_paths.clip_sidecar_path(vault_root),
        index_paths.governance_sidecar_path(vault_root),
        claims.sidecar_path(vault_root),
        memory_refs.sidecar_path(vault_root),
        lexstore.lexical_path(vault_root),
        epistemic_graph.sidecar_path(vault_root),
        deferred_index.store_path(vault_root),
        media_jobs.job_store_path(vault_root),
    )
    for sidecar in sidecars:
        if candidate in (
            sidecar,
            sidecar.with_name(f"{sidecar.name}-wal"),
            sidecar.with_name(f"{sidecar.name}-shm"),
            sidecar.with_name(f"{sidecar.name}-journal"),
        ):
            return True

    if candidate in (
        media_jobs.worker_lock_path(vault_root),
        voice_profiles.voice_profiles_path(vault_root),
        review_state.state_path(vault_root),
        graph_sync.checkpoint_path(vault_root),
        graph_sync.floor_path(vault_root),
    ):
        return True
    review_state_path = review_state.state_path(vault_root)
    if (
        candidate.parent == review_state_path.parent
        and _REVIEW_STATE_TEMP_RE.fullmatch(candidate.name) is not None
    ):
        return True
    lexical_path = lexstore.lexical_path(vault_root)
    if (
        candidate.parent == lexical_path.parent
        and _LEXICAL_REBUILD_TEMP_RE.fullmatch(candidate.name) is not None
    ):
        return True
    receipt_root = graph_sync.graph_commit_receipt_path(vault_root, "0" * 24).parent
    graph_sidecar_parent = epistemic_graph.sidecar_path(vault_root).parent
    return candidate.is_relative_to(receipt_root) or (
        candidate.parent == graph_sidecar_parent
        and _GRAPH_REBUILD_SIDECAR_RE.fullmatch(candidate.name) is not None
    )


def _membership_manifest(
    vault_root: Path,
    current: policy_module.Policy,
    prospective: policy_module.Policy,
    document_paths: set[str],
) -> list[dict[str, str]]:
    affected = _affected_scope_ids(current, prospective, document_paths)
    rows: dict[str, str] = {}
    if affected:
        for candidate in sorted((vault_root / kb_dirname()).rglob("*")):
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(vault_root).as_posix()
            if policy_module.is_governance_path(rel):
                continue
            if _is_operational_membership_path(vault_root, candidate):
                continue
            union = _memberships_for_path(
                vault_root, candidate, current
            ) | _memberships_for_path(vault_root, candidate, prospective)
            if union & affected:
                rows[rel] = _content_hash(candidate)

    # Release proposals bind concrete bridge/dependency identities, not scope
    # membership.  Include both their declared paths and every live page that
    # carries one of those IDs so a byte edit, rename, deletion, or newly
    # ambiguous duplicate invalidates the reviewed proposal before commit.
    releases = [
        release
        for candidate in (current, prospective)
        for release in candidate.release_grants
        if release.source in document_paths
    ]
    expected_paths = {
        path
        for release in releases
        for path in (release.path, *(dependency.path for dependency in release.bridge_of))
    }
    wanted_ids = {
        identity
        for release in releases
        for ref in (release.ref, *(dependency.ref for dependency in release.bridge_of))
        if (identity := memory_refs.parse_memory_ref(ref)) is not None
    }
    for rel in expected_paths:
        rows[rel] = _content_hash(vault_root / rel)
    if wanted_ids:
        for candidate in sorted(vault_root.rglob("*.md")):
            if not candidate.is_file():
                continue
            try:
                parsed = find_corpus.parse_page(
                    candidate,
                    candidate.stat().st_mtime,
                    vault_root,
                )
            except OSError:
                parsed = None
            if parsed is None:
                continue
            identity = memory_refs.normalize_id(parsed.frontmatter.get("exomem_id"))
            if identity in wanted_ids:
                rows[parsed.rel_path] = _content_hash(candidate)
    return [
        {"path": rel, "content_hash": rows[rel]}
        for rel in sorted(rows)
    ]


def _resolved_membership_manifest(
    vault_root: Path, candidate: policy_module.Policy, paths: list[str] | tuple[str, ...]
) -> list[dict[str, Any]]:
    return [
        {
            "path": rel,
            "scope_ids": sorted(
                _memberships_for_path(vault_root, vault_root / rel, candidate)
            ),
        }
        for rel in paths
    ]


def _compared_audiences(
    current: policy_module.Policy, prospective: policy_module.Policy
) -> list[str]:
    """Every audience a transition can move, including the unnamed default.

    Enumerating rules and grants alone compares only audiences some document
    names — which is exactly the set a change to the DEFAULT cannot appear in.
    Removing a scope's `default_deny` then moves an unnamed audience from 0 to
    6 while every named audience sits still, and the review reports a
    narrowing. `UNNAMED_AUDIENCE_PROBE` stands in for that audience; no
    document can name it and no credential can mint it, so it always resolves
    through the default and never matches a rule or grant.

    It also guarantees a non-empty audience set, so an empty lattice can no
    longer be mistaken for "nothing to compare".
    """
    return sorted(
        {
            document.audience
            for policy in (current, prospective)
            for document in (*policy.rules, *policy.grants)
        }
        | {UNNAMED_AUDIENCE_PROBE}
    )


def _proposal_analysis(
    vault_root: Path,
    current: policy_module.Policy,
    prospective: policy_module.Policy,
    manifest: list[dict[str, str]],
) -> tuple[
    dict[str, int], list[str], str, list[str], bool, int | None, int | None
]:
    """Evaluate the concrete affected membership, never caller hint paths."""
    audiences = _compared_audiences(current, prospective)
    purposes = sorted(
        {rule.purpose for policy in (current, prospective) for rule in policy.rules if rule.purpose}
    )
    purposes = [None, *purposes]
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    rule_ids: set[str] = set()
    # `target_ceiling` remains the highest level the proposal leaves an
    # AUTHORED audience at. The default has its own owner-visible ceiling so the
    # rotation path is explicit without changing that compatibility field.
    named_after: list[int] = []
    unnamed_after: list[int] = []
    all_open = True
    for row in manifest:
        rel = str(row["path"])
        try:
            current_scopes = _memberships_for_path(vault_root, vault_root / rel, current)
            prospective_scopes = _memberships_for_path(vault_root, vault_root / rel, prospective)
        except GovernanceError:
            return (
                {"narrowed": 0, "widened": 0, "unchanged": 0},
                [],
                "widening",
                [],
                False,
                None,
                None,
            )
        for audience in audiences:
            for purpose in purposes:
                key = f"{audience}:{purpose or '-'}:{rel}"
                old = decisions.decide(current_scopes, audience=audience, purpose=purpose, policy=current)
                new = decisions.decide(prospective_scopes, audience=audience, purpose=purpose, policy=prospective)
                before[key] = old.level
                after[key] = new.level
                if audience != UNNAMED_AUDIENCE_PROBE:
                    named_after.append(new.level)
                else:
                    unnamed_after.append(new.level)
                rule_ids.update(old.rule_ids)
                rule_ids.update(new.rule_ids)
                rule_ids.update(
                    grant.id for grant in (*current.grants, *prospective.grants)
                    if grant.audience == audience and bool(set(grant.scope_ids) & (set(current_scopes) | set(prospective_scopes)))
                )
                all_open = all_open and old.level == policy_module.DISCLOSURE_MAX and new.level == policy_module.DISCLOSURE_MAX and old.release_reason is None and new.release_reason is None
    consequences = {
        "narrowed": sum(after[key] < before[key] for key in before),
        "widened": sum(after[key] > before[key] for key in before),
        "unchanged": sum(after[key] == before[key] for key in before),
    }
    direction = classify_transition_direction(before, after)
    samples = [str(row["path"]) for row in manifest[:5]] if all_open else []
    return (
        consequences,
        samples,
        direction,
        sorted(rule_ids),
        all_open,
        max(named_after, default=None),
        max(unnamed_after, default=None),
    )


def _purpose_direction(
    vault_root: Path, *, audience: str, before_purpose: str | None, after_purpose: str
) -> str:
    policy = policy_module.load(vault_root)
    if policy.blocked:
        return "widening"
    conn = store.open_readonly_connection(vault_root)
    if conn is not None:
        try:
            active = conn.execute(
                "SELECT 1 FROM governance_session_grants WHERE audience=? AND status='active' LIMIT 1",
                (audience,),
            ).fetchone()
        finally:
            conn.close()
        if active is not None:
            return "widening"
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    try:
        for candidate in sorted((vault_root / kb_dirname()).rglob("*")):
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(vault_root).as_posix()
            if policy_module.is_governance_path(rel):
                continue
            if _is_operational_membership_path(vault_root, candidate):
                continue
            scopes = _memberships_for_path(vault_root, candidate, policy)
            before[rel] = decisions.decide(
                scopes, audience=audience, purpose=before_purpose, policy=policy
            ).level
            after[rel] = decisions.decide(
                scopes, audience=audience, purpose=after_purpose, policy=policy
            ).level
    except GovernanceError:
        return "widening"
    return classify_transition_direction(before, after)


def _proposal(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    _require_owner(kwargs.get("principal"))
    documents = _canonical_documents(kwargs.get("documents"))
    raw_patterns = kwargs.get("selector_paths") or []
    if not isinstance(raw_patterns, list) or not all(
        isinstance(pattern, str) and pattern for pattern in raw_patterns
    ):
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL", "selector_paths must be a list of strings"
        )
    patterns = sorted(set(raw_patterns))
    current_policy = policy_module.load(vault_root)
    if current_policy.blocked:
        raise GovernanceError("GOVERNANCE_BLOCKED", "current policy cannot be evaluated")
    prospective = _prospective_policy(vault_root, documents)
    if prospective.blocked:
        raise GovernanceError(
            "INVALID_GOVERNANCE_POLICY",
            _canonical_json(list(prospective.findings)),
        )
    manifest = _membership_manifest(
        vault_root, current_policy, prospective, set(documents)
    )
    now = float(kwargs.get("now", time.time()))
    expires_at = now + max(1, int(kwargs.get("ttl_seconds", DEFAULT_PROPOSAL_TTL_SECONDS)))
    intent = str(kwargs.get("intent") or "").strip()
    if not intent:
        raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "intent is required")
    target_ceiling = int(kwargs.get("target_ceiling", policy_module.DISCLOSURE_MAX))
    if not policy_module.DISCLOSURE_MIN <= target_ceiling <= policy_module.DISCLOSURE_MAX:
        raise GovernanceError("INVALID_GOVERNANCE_PROPOSAL", "target ceiling is invalid")
    (
        consequences,
        samples,
        direction,
        overlaps,
        all_open,
        derived_ceiling,
        unnamed_audience_ceiling,
    ) = _proposal_analysis(vault_root, current_policy, prospective, manifest)
    hint_diagnostics: list[str] = []
    if patterns:
        hint_diagnostics.append("selector_paths are compatibility hints; concrete membership is authoritative")
    if derived_ceiling is None or target_ceiling != derived_ceiling:
        hint_diagnostics.append("target_ceiling is a compatibility hint, not an authorization fact")
    proposal_id = uuid.uuid4().hex
    payload = {
        "interpretation": intent,
        "documents": documents,
        "duration": kwargs.get("duration"),
    }
    conn = store.open_connection(vault_root)
    try:
        conn.execute(
            "INSERT INTO governance_proposals "
            "(proposal_id, created_at, expires_at, proposal_json, fingerprint_at_propose, "
            "membership_manifest, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (
                proposal_id,
                now,
                expires_at,
                _canonical_json(payload),
                current_policy.fingerprint,
                _canonical_json(manifest),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "interpretation": intent,
        "canonical_yaml": documents,
        "membership_preview": {"count": len(manifest), "samples": samples},
        "consequences": {
            **consequences,
            "target_ceiling": derived_ceiling,
            "unnamed_audience_ceiling": unnamed_audience_ceiling,
            "direction": direction,
        },
        "overlaps": overlaps,
        "hint_diagnostics": hint_diagnostics,
        "duration": kwargs.get("duration"),
        "reversal": "undo",
        "proposal_id": proposal_id,
        "expires_at": expires_at,
    }


def classify_transition_direction(
    before: Mapping[str, int] | None, after: Mapping[str, int] | None
) -> str:
    """Only a complete pointwise proof can classify a transition narrowing."""
    if before is None or after is None or set(before) != set(after):
        return "widening"
    if any(not isinstance(level, int) for level in (*before.values(), *after.values())):
        return "widening"
    return "narrowing" if all(after[key] <= before[key] for key in before) else "widening"


def _component(
    kind: str, key: str, value: Mapping[str, Any], *, status: str
) -> dict[str, Any]:
    normalized = dict(value)
    if (
        kind in {"token", "grant", "dependent_grant", "purpose", "proposal"}
        and normalized.get("status") != "absent"
        and "projection_version" not in normalized
    ):
        normalized = authorization_row(**normalized)
    return {
        "component_kind": kind,
        "component_key": key,
        "value_json": _canonical_json(normalized),
        "value_hash": _digest(normalized),
        "status": status,
    }


def _composite(phase: str, components: list[dict[str, Any]]) -> str:
    normalized = [
        {
            "kind": item["component_kind"],
            "key": item["component_key"],
            "value_hash": item["value_hash"],
            "status": item["status"],
        }
        for item in sorted(
            components,
            key=lambda row: (row["component_kind"], row["component_key"]),
        )
    ]
    return _digest({"domain": f"governance-composite/{phase}/v1", "components": normalized})


def _archive_value(rel: str, prior_bytes: bytes | None, prior_hash: str) -> dict[str, str]:
    return {
        "path_hash": hashlib.sha256(rel.encode()).hexdigest(),
        "prior_hash": prior_hash,
        "bytes_hash": (
            "absent" if prior_bytes is None else hashlib.sha256(prior_bytes).hexdigest()
        ),
    }


def _commit_event_id(
    proposal_id: str, attempt_no: int, attempt_nonce: str, prepared_digest: str
) -> str:
    return receipts.critical_event_id(
        {
            "operation": "governance_policy_commit",
            "proposal_id": proposal_id,
            "attempt": attempt_no,
            "attempt_nonce": attempt_nonce,
            "prepared": prepared_digest,
        }
    )


def _marker_path(vault_root: Path) -> Path:
    return policy_target(policy_module.governance_root(vault_root), PENDING_MARKER)


def _durable_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _durable_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _clear_policy_caches(vault_root: Path) -> None:
    policy_module._CACHE.pop(str(policy_module.governance_root(vault_root)), None)
    membership.clear_memo()
    try:
        from . import egress

        egress.clear_decision_memo()
    except ImportError:  # pragma: no cover - package is complete in production
        pass


def _insert_components(
    conn: sqlite3.Connection,
    event_id: str,
    phases: Mapping[str, list[dict[str, Any]]],
) -> None:
    for phase, rows in phases.items():
        for ordinal, row in enumerate(
            sorted(rows, key=lambda item: (item["component_kind"], item["component_key"]))
        ):
            conn.execute(
                "INSERT OR IGNORE INTO governance_operation_components "
                "(event_id, phase, ordinal, component_kind, component_key, value_json, "
                "value_hash, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    phase,
                    ordinal,
                    row["component_kind"],
                    row["component_key"],
                    row["value_json"],
                    row["value_hash"],
                    row["status"],
                ),
            )


def _create_journal(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    operation: str,
    who: RequestPrincipal,
    authorization_session: str | None,
    direction: str,
    phases: Mapping[str, list[dict[str, Any]]],
    child_ids: list[str],
    marker_required: bool = False,
    proposal_id: str | None = None,
    phase: str = "pending",
    now: float,
) -> dict[str, str]:
    digests = {phase: _composite(phase, rows) for phase, rows in phases.items()}
    affected_ids = sorted(
        {
            hashlib.sha256(
                f"{row['component_kind']}:{row['component_key']}".encode()
            ).hexdigest()
            for rows in phases.values()
            for row in rows
        }
    )
    conn.execute(
        "INSERT INTO governance_operation_journals "
        "(event_id, operation, causation_id, authorization_session, principal_id, phase, "
        "direction, prior_digest, prepared_digest, final_digest, affected_ids, "
        "required_child_intents, required_child_terminals, proposal_id, attempt_no, "
        "marker_required, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
        (
            event_id,
            operation,
            event_id,
            authorization_session,
            who.audience_id,
            phase,
            direction,
            digests["prior"],
            digests["prepared"],
            digests["final"],
            _canonical_json(affected_ids),
            _canonical_json(child_ids),
            _canonical_json([f"{child}:committed" for child in child_ids]),
            proposal_id,
            1 if marker_required else 0,
            now,
            now,
        ),
    )
    _insert_components(conn, event_id, phases)
    return digests


def _arm_journal(vault_root: Path, event_id: str, *, now: float) -> None:
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "UPDATE governance_operation_journals SET phase='pending', updated_at=? "
            "WHERE event_id=? AND phase='allocating'",
            (now, event_id),
        )
        if cursor.rowcount != 1:
            raise GovernanceError("GOVERNANCE_BLOCKED", "control journal could not arm")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _standing_grant_relative_path(vault_root: Path, raw_grant_id: Any) -> tuple[str, str]:
    """Validate a canonical grant id and prove its target is one direct child."""
    if not policy_module.is_valid_document_id(raw_grant_id):
        raise GovernanceError(
            "INVALID_STANDING_GRANT_ID",
            "grant_id must be a canonical 26-character Crockford-base32 ULID",
        )
    grant_id = str(raw_grant_id)
    try:
        governance_root = policy_module.governance_root(vault_root).resolve(strict=False)
        target = policy_target(
            policy_module.governance_root(vault_root), f"grants/{grant_id}.yaml"
        )
        grants_root = target.parent
    except (OSError, RuntimeError) as exc:
        raise GovernanceError(
            "INVALID_STANDING_GRANT_ID", "standing grant target cannot be resolved safely"
        ) from exc
    if (
        grants_root.parent != governance_root
        or target.name != f"{grant_id}.yaml"
    ):
        raise GovernanceError(
            "INVALID_STANDING_GRANT_ID",
            "standing grant target must be a direct child of _Governance/grants",
        )
    return grant_id, f"grants/{grant_id}.yaml"


def _standing_grant(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who = _require_owner(kwargs.get("principal"))
    grant_id, rel = _standing_grant_relative_path(vault_root, kwargs.get("grant_id"))
    reconciliation = reconcile_governance_operations(vault_root)
    if reconciliation["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    store.require_authoring_schema(vault_root)
    raw_scope_ids = kwargs.get("scope_ids")
    audience = str(kwargs.get("audience") or "").strip()
    ceiling = kwargs.get("ceiling")
    if (
        not isinstance(raw_scope_ids, list)
        or not raw_scope_ids
        or not all(isinstance(value, str) and value for value in raw_scope_ids)
        or not audience
        or not isinstance(ceiling, int)
        or isinstance(ceiling, bool)
        or not policy_module.DISCLOSURE_MIN <= ceiling <= policy_module.DISCLOSURE_MAX
    ):
        raise GovernanceError("INVALID_STANDING_GRANT", "grant fields are invalid")
    current = policy_module.load(vault_root)
    if current.blocked:
        raise GovernanceError("GOVERNANCE_BLOCKED", "current policy cannot be evaluated")
    if grant_id in {grant.id for grant in current.grants}:
        raise GovernanceError("GRANT_EXISTS", "standing grant already exists")
    if not set(raw_scope_ids) <= set(current.scopes):
        raise GovernanceError("SCOPE_UNKNOWN", "one or more scopes do not exist")
    document = yaml.safe_dump(
        {
            "governance_version": 1,
            "id": grant_id,
            "scope_ids": sorted(set(raw_scope_ids)),
            "audience": audience,
            "ceiling": ceiling,
        },
        sort_keys=False,
        allow_unicode=True,
    )
    prospective = _prospective_policy(vault_root, {rel: document})
    if prospective.blocked:
        raise GovernanceError(
            "INVALID_STANDING_GRANT", _canonical_json(list(prospective.findings))
        )
    transition = kwargs["_selection"]
    result = _yaml_transition(
        vault_root,
        transition=transition,
        who=who,
        documents={rel: document},
        direction=_effective_transition_direction(vault_root, {rel: document}),
        now=float(kwargs.get("now", time.time())),
    )
    return {**result, "grant_id": grant_id}


def _standing_revoke(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who = _require_owner(kwargs.get("principal"))
    grant_id, _rel = _standing_grant_relative_path(vault_root, kwargs.get("grant_id"))
    reconciliation = reconcile_governance_operations(vault_root)
    if reconciliation["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    store.require_authoring_schema(vault_root)
    current = policy_module.load(vault_root)
    match = next((grant for grant in current.grants if grant.id == grant_id), None)
    if match is None:
        raise GovernanceError("GRANT_UNKNOWN", "standing grant does not exist")
    return _yaml_transition(
        vault_root,
        transition=kwargs["_selection"],
        who=who,
        documents={match.source: None},
        direction=_effective_transition_direction(vault_root, {match.source: None}),
        now=float(kwargs.get("now", time.time())),
    )


def _token_projection(row: tuple[Any, ...], *, consumed_at: Any, status: str, prepared_event_id: Any) -> dict[str, Any]:
    return authorization_row(
        audience=str(row[0]),
        max_level=int(row[1]),
        fingerprints=str(row[2]),
        paths=str(row[3]),
        expires_at=float(row[4]),
        minted_at=float(row[5]),
        consumed_at=consumed_at,
        authorization_session=str(row[7]),
        purpose=row[8],
        org_ceiling=int(row[9]),
        status=status,
        prepared_event_id=prepared_event_id,
    )


def _grant_projection(
    *,
    authorization_session: str,
    audience: str,
    purpose: str | None,
    ceiling: int,
    paths: str,
    fingerprints: str,
    token_jti: str,
    status: str,
    prepared_event_id: str | None,
    created_at: float,
    expires_at: float,
    revoked_at: float | None,
    membership_manifest: str,
    policy_fingerprint: str,
) -> dict[str, Any]:
    return authorization_row(
        authorization_session=authorization_session,
        audience=audience,
        purpose=purpose,
        ceiling=ceiling,
        paths=paths,
        fingerprints=fingerprints,
        token_jti=token_jti,
        status=status,
        prepared_event_id=prepared_event_id,
        created_at=created_at,
        expires_at=expires_at,
        revoked_at=revoked_at,
        membership_manifest=membership_manifest,
        policy_fingerprint=policy_fingerprint,
    )


def _grant(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    selection = kwargs["_selection"]
    who, authorization_session = _require_authorization_session(
        kwargs.get("principal"), kwargs.get("authorization_session")
    )
    reconcile = reconcile_governance_operations(vault_root)
    if reconcile["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    store.require_authoring_schema(vault_root)
    token = str(kwargs.get("token") or "")
    now = float(kwargs.get("now", time.time()))
    purpose = kwargs.get("purpose")
    if purpose is None:
        purpose = who.purpose
    if purpose is None:
        purpose = store.active_session_purpose(
            vault_root,
            audience=who.audience_id,
            authorization_session=authorization_session,
            now=now,
        )
    try:
        claim = tokens_module.verify(
            vault_root,
            token,
            audience=who.audience_id,
            authorization_session=authorization_session,
            purpose=purpose,
            now=int(now),
        )
        tokens_module._check_content(vault_root, claim)
    except tokens_module.WithholdTokenError as exc:
        raise GovernanceError(exc.code, exc.reason) from exc
    duration = max(1, int(kwargs.get("duration_seconds", 3600)))
    expires_at = min(float(claim.expires_at), now + duration)
    bound_policy = policy_module.load(vault_root)
    if bound_policy.blocked:
        raise GovernanceError("GOVERNANCE_BLOCKED", "current policy cannot be evaluated")
    membership_manifest = _resolved_membership_manifest(
        vault_root, bound_policy, claim.paths
    )

    conn = store.open_connection(vault_root)
    try:
        token_row = conn.execute(
            "SELECT audience, max_level, fingerprints, paths, expires_at, minted_at, consumed_at, "
            "authorization_session, purpose, org_ceiling, status, prepared_event_id "
            "FROM withhold_tokens WHERE jti=?",
            (claim.jti,),
        ).fetchone()
    finally:
        conn.close()
    if token_row is None:
        raise GovernanceError("TOKEN_UNKNOWN", "no such escalation token")
    if token_row[6] is not None:
        raise GovernanceError("TOKEN_CONSUMED", "this escalation was already used")
    if token_row[11] is not None:
        raise GovernanceError("TOKEN_RESERVED", "this escalation has an open operation")
    nonce = uuid.uuid4().hex
    causation_id = receipts.critical_event_id(
        {
            "operation": "governance_session_grant",
            "jti": claim.jti,
            "authorization_session": authorization_session,
            "nonce": nonce,
        }
    )
    grant_id = hashlib.sha256(f"grant:{causation_id}:{claim.jti}".encode()).hexdigest()
    child_ids = [
        receipts.critical_event_id(
            {"causation_id": causation_id, "intent": f"child-{index}"}
        )
        for index, _receipt in enumerate(selection.child_receipts, start=1)
    ]
    grant_paths = _canonical_json(list(claim.paths))
    grant_fingerprints = _canonical_json(list(claim.fingerprints))
    grant_membership = _canonical_json(membership_manifest)
    grant_prepared = _grant_projection(
        authorization_session=authorization_session, audience=who.audience_id,
        purpose=purpose, ceiling=claim.max_level, paths=grant_paths,
        fingerprints=grant_fingerprints, token_jti=claim.jti, status="prepared",
        prepared_event_id=causation_id, created_at=now, expires_at=expires_at,
        revoked_at=None, membership_manifest=grant_membership,
        policy_fingerprint=bound_policy.fingerprint,
    )
    phases = {
        "prior": [
            _component(
                "token",
                claim.jti,
                _token_projection(token_row, consumed_at=None, status="active", prepared_event_id=None),
                status="active",
            ),
            _component("grant", grant_id, {"status": "absent"}, status="absent"),
        ],
        "prepared": [
            _component(
                "token",
                claim.jti,
                _token_projection(token_row, consumed_at=now, status="prepared", prepared_event_id=causation_id),
                status="prepared",
            ),
            _component(
                "grant",
                grant_id,
                grant_prepared,
                status="prepared",
            ),
        ],
        "final": [
            _component(
                "token",
                claim.jti,
                _token_projection(token_row, consumed_at=now, status="consumed", prepared_event_id=None),
                status="consumed",
            ),
            _component(
                "grant",
                grant_id,
                {**grant_prepared, "status": "active", "prepared_event_id": None},
                status="active",
            ),
        ],
    }
    digests = {phase: _composite(phase, rows) for phase, rows in phases.items()}
    operations = selection.child_receipts
    affected = sorted(
        hashlib.sha256(
            f"{component['component_kind']}:{component['component_key']}".encode()
        ).hexdigest()
        for component in phases["prepared"]
    )
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        token_row = conn.execute(
            "SELECT consumed_at, prepared_event_id FROM withhold_tokens WHERE jti=?",
            (claim.jti,),
        ).fetchone()
        if token_row is None or token_row[0] is not None or token_row[1] is not None:
            raise GovernanceError("TOKEN_CONSUMED", "this escalation is no longer available")
        persisted = _create_journal(
            conn,
            event_id=causation_id,
            operation=selection.journal_operation,
            who=who,
            authorization_session=authorization_session,
            direction="widening",
            phases=phases,
            child_ids=child_ids,
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    for index, (child_id, child_operation) in enumerate(
        zip(child_ids, operations, strict=True), start=1
    ):
        receipts.begin_event(
            vault_root,
            operation=child_operation,
            prior=digests["prior"],
            prepared=digests["prepared"],
            target=digests["final"],
            affected_ids=affected,
            event_id=child_id,
            parent_causation_id=causation_id,
            intent_id=f"child-{index}",
        )
        if kwargs.get("crash_at") == f"after_child_intent:{index}":
            raise GovernanceCrash(f"after_child_intent:{index}")
    _arm_journal(vault_root, causation_id, now=now)

    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "UPDATE withhold_tokens SET consumed_at=?, status='prepared', prepared_event_id=? "
            "WHERE jti=? AND consumed_at IS NULL AND prepared_event_id IS NULL",
            (now, causation_id, claim.jti),
        )
        if cursor.rowcount != 1:
            raise GovernanceError("TOKEN_CONSUMED", "this escalation was already used")
        conn.execute(
            "INSERT INTO governance_session_grants "
            "(grant_id, authorization_session, audience, purpose, ceiling, paths, fingerprints, "
            "token_jti, status, prepared_event_id, created_at, expires_at, "
            "membership_manifest, policy_fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?)",
            (
                grant_id,
                authorization_session,
                who.audience_id,
                purpose,
                claim.max_level,
                grant_paths,
                grant_fingerprints,
                claim.jti,
                causation_id,
                now,
                expires_at,
                grant_membership,
                bound_policy.fingerprint,
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    if kwargs.get("crash_at") == "after_compound_state":
        raise GovernanceCrash("after_compound_state")
    for index, child_id in enumerate(child_ids, start=1):
        receipts.commit_event(vault_root, child_id, outcome="prepared")
        if kwargs.get("crash_at") == f"after_child_terminal:{index}":
            raise GovernanceCrash(f"after_child_terminal:{index}")
    _activate_event(vault_root, causation_id, remove_marker=False, now=now)
    return {
        "status": "committed",
        "causation_id": causation_id,
        "grant_id": grant_id,
    }


def _single_sidecar_transition(
    vault_root: Path,
    *,
    operation: str,
    who: RequestPrincipal,
    authorization_session: str,
    phases: Mapping[str, list[dict[str, Any]]],
    prepare: Any,
    direction: str,
    now: float,
) -> str:
    event_id = receipts.critical_event_id(
        {
            "operation": operation,
            "authorization_session": authorization_session,
            "nonce": uuid.uuid4().hex,
            "prepared": _composite("prepared", phases["prepared"]),
        }
    )
    digests = {phase: _composite(phase, rows) for phase, rows in phases.items()}
    transition = operation_variant(operation)
    journal_operation = transition.journal_operation
    if journal_operation is None:
        raise GovernanceError("GOVERNANCE_BLOCKED", "operation has no journal identity")
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        persisted = _create_journal(
            conn,
            event_id=event_id,
            operation=journal_operation,
            who=who,
            authorization_session=authorization_session,
            direction=direction,
            phases=phases,
            child_ids=[event_id],
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    receipts.begin_event(
        vault_root,
        operation=transition.receipt_event,
        prior=digests["prior"],
        prepared=digests["prepared"],
        target=digests["final"],
        affected_ids=sorted({
            hashlib.sha256(
                f"{row['component_kind']}:{row['component_key']}".encode()
            ).hexdigest()
            for row in phases["prepared"]
        }),
        event_id=event_id,
    )
    _arm_journal(vault_root, event_id, now=now)
    prepare(event_id)
    receipts.commit_event(vault_root, event_id, outcome="prepared")
    _activate_event(vault_root, event_id, remove_marker=False, now=now)
    return event_id


def _declare(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who, authorization_session = _require_authorization_session(
        kwargs.get("principal"), kwargs.get("authorization_session")
    )
    reconcile = reconcile_governance_operations(vault_root)
    if reconcile["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    purpose = str(kwargs.get("purpose") or "").strip()
    if not purpose or len(purpose) > 256:
        raise GovernanceError("INVALID_PURPOSE", "purpose is required")
    now = float(kwargs.get("now", time.time()))
    expires_at = now + max(1, int(kwargs.get("duration_seconds", 3600)))
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT authorization_session, principal_id, purpose, status, prepared_event_id, "
            "created_at, expires_at "
            "FROM governance_session_purpose "
            "WHERE authorization_session=?",
            (authorization_session,),
        ).fetchone()
    finally:
        conn.close()
    prior_value = (
        {"status": "absent"}
        if row is None
        else authorization_row(
            authorization_session=str(row[0]), principal_id=str(row[1]),
            purpose=str(row[2]), status=str(row[3]), prepared_event_id=row[4],
            created_at=float(row[5]), expires_at=float(row[6]),
        )
    )
    prior_purpose = (
        str(row[2])
        if row is not None and str(row[3]) == "active" and float(row[6]) >= now
        else None
    )
    direction = _purpose_direction(
        vault_root,
        audience=who.audience_id,
        before_purpose=prior_purpose,
        after_purpose=purpose,
    )
    phases = {
        "prior": [_component("purpose", authorization_session, prior_value, status="prior")],
        "prepared": [
            _component(
                "purpose",
                authorization_session,
                authorization_row(
                    authorization_session=authorization_session, principal_id=who.audience_id,
                    purpose=purpose, status="prepared", prepared_event_id="EVENT",
                    created_at=now, expires_at=expires_at,
                ),
                status="prepared",
            )
        ],
        "final": [
            _component(
                "purpose",
                authorization_session,
                authorization_row(
                    authorization_session=authorization_session, principal_id=who.audience_id,
                    purpose=purpose, status="active", prepared_event_id=None,
                    created_at=now, expires_at=expires_at,
                ),
                status="active",
            )
        ],
    }
    # Event ids are preallocated before final component rows; substitute the
    # stable id once, then persist the immutable component set.
    event_id = receipts.critical_event_id(
        {
            "operation": "declare",
            "authorization_session": authorization_session,
            "purpose": purpose,
            "nonce": uuid.uuid4().hex,
        }
    )
    phases["prepared"] = [
        _component(
            "purpose",
            authorization_session,
            authorization_row(
                authorization_session=authorization_session, principal_id=who.audience_id,
                purpose=purpose, status="prepared", prepared_event_id=event_id,
                created_at=now, expires_at=expires_at,
            ),
            status="prepared",
        )
    ]

    def prepare(prepared_event: str) -> None:
        conn = store.open_connection(vault_root)
        try:
            conn.execute(
                "INSERT INTO governance_session_purpose_staging "
                "(event_id, authorization_session, principal_id, purpose, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_id) DO UPDATE SET authorization_session=excluded.authorization_session, "
                "principal_id=excluded.principal_id, purpose=excluded.purpose, "
                "created_at=excluded.created_at, expires_at=excluded.expires_at",
                (
                    prepared_event,
                    authorization_session,
                    who.audience_id,
                    purpose,
                    now,
                    expires_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    digests = {phase: _composite(phase, rows) for phase, rows in phases.items()}
    # Inline the generic transition so the preallocated event id is retained.
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        persisted = _create_journal(
            conn,
            event_id=event_id,
            operation=kwargs["_selection"].journal_operation,
            who=who,
            authorization_session=authorization_session,
            direction=direction,
            phases=phases,
            child_ids=[event_id],
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    receipts.begin_event(
        vault_root,
        operation=kwargs["_selection"].receipt_event,
        prior=digests["prior"],
        prepared=digests["prepared"],
        target=digests["final"],
        affected_ids=sorted({
            hashlib.sha256(
                f"{row['component_kind']}:{row['component_key']}".encode()
            ).hexdigest()
            for row in phases["prepared"]
        }),
        event_id=event_id,
    )
    _arm_journal(vault_root, event_id, now=now)
    prepare(event_id)
    if kwargs.get("crash_at") == "after_purpose_prepare":
        raise GovernanceCrash("after_purpose_prepare")
    receipts.commit_event(vault_root, event_id, outcome="prepared")
    _activate_event(vault_root, event_id, remove_marker=False, now=now)
    return {
        "status": "committed",
        "event_id": event_id,
        "purpose": purpose,
        "direction": direction,
    }


def _revoke(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    selection = kwargs["_selection"]
    if kwargs.get("scope") != "session":
        raise GovernanceError("INVALID_REVOKE_SCOPE", "scope must be session or standing")
    who, authorization_session = _require_authorization_session(
        kwargs.get("principal"), kwargs.get("authorization_session")
    )
    reconcile = reconcile_governance_operations(vault_root)
    if reconcile["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    now = float(kwargs.get("now", time.time()))
    conn = store.open_connection(vault_root)
    try:
        rows = conn.execute(
            "SELECT grant_id, authorization_session, audience, purpose, ceiling, paths, fingerprints, "
            "token_jti, status, prepared_event_id, created_at, expires_at, revoked_at, "
            "membership_manifest, policy_fingerprint FROM governance_session_grants WHERE authorization_session=? "
            "AND audience=? AND status='active' ORDER BY grant_id",
            (authorization_session, who.audience_id),
        ).fetchall()
    finally:
        conn.close()
    grant_values = {
        str(row[0]): _grant_projection(
            authorization_session=str(row[1]), audience=str(row[2]), purpose=row[3], ceiling=int(row[4]),
            paths=str(row[5]), fingerprints=str(row[6]), token_jti=str(row[7]), status=str(row[8]),
            prepared_event_id=row[9], created_at=float(row[10]), expires_at=float(row[11]),
            revoked_at=row[12], membership_manifest=str(row[13]), policy_fingerprint=str(row[14]),
        ) for row in rows
    }
    grant_ids = sorted(grant_values)
    if not grant_ids:
        return {"status": "committed", "revoked": 0}
    event_id = receipts.critical_event_id(
        {"operation": "revoke", "grants": grant_ids, "nonce": uuid.uuid4().hex}
    )
    phases = {
        "prior": [
            _component(
                "grant",
                grant_id,
                grant_values[grant_id],
                status="active",
            )
            for grant_id in grant_ids
        ],
        "prepared": [
            _component(
                "grant",
                grant_id,
                {**grant_values[grant_id], "status": "prepared_revoke", "prepared_event_id": event_id},
                status="prepared_revoke",
            )
            for grant_id in grant_ids
        ],
        "final": [
            _component(
                "grant",
                grant_id,
                {**grant_values[grant_id], "status": "revoked", "prepared_event_id": None, "revoked_at": now},
                status="revoked",
            )
            for grant_id in grant_ids
        ],
    }
    digests = {phase: _composite(phase, values) for phase, values in phases.items()}
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        persisted = _create_journal(
            conn,
            event_id=event_id,
            operation=selection.journal_operation,
            who=who,
            authorization_session=authorization_session,
            direction="narrowing",
            phases=phases,
            child_ids=[event_id],
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    receipts.begin_event(
        vault_root,
        operation=selection.receipt_event,
        prior=digests["prior"],
        prepared=digests["prepared"],
        target=digests["final"],
        affected_ids=sorted({
            hashlib.sha256(
                f"{row['component_kind']}:{row['component_key']}".encode()
            ).hexdigest()
            for row in phases["prepared"]
        }),
        event_id=event_id,
    )
    _arm_journal(vault_root, event_id, now=now)
    conn = store.open_connection(vault_root)
    try:
        conn.execute(
            "UPDATE governance_session_grants SET status='prepared_revoke', prepared_event_id=? "
            "WHERE authorization_session=? AND audience=? AND status='active'",
            (event_id, authorization_session, who.audience_id),
        )
        conn.commit()
    finally:
        conn.close()
    receipts.commit_event(vault_root, event_id, outcome="prepared")
    _activate_event(vault_root, event_id, remove_marker=False, now=now)
    return {"status": "committed", "event_id": event_id, "revoked": len(grant_ids)}


def _effective_transition_direction(
    vault_root: Path, documents: Mapping[str, str | None]
) -> str:
    """Classify a YAML transition from its complete effective decision lattice."""
    current = policy_module.load(vault_root)
    if current.blocked:
        return "widening"
    try:
        prospective = _prospective_policy(vault_root, documents)
    except (GovernanceError, OSError, TypeError, ValueError):
        return "widening"
    if prospective.blocked or current.release_grants != prospective.release_grants:
        return "widening"
    try:
        manifest = _membership_manifest(vault_root, current, prospective, set(documents))
        audiences = _compared_audiences(current, prospective)
        purposes = [
            None,
            *sorted(
                {
                    rule.purpose
                    for policy in (current, prospective)
                    for rule in policy.rules
                    if rule.purpose is not None
                }
            ),
        ]
        before: dict[str, int] = {}
        after: dict[str, int] = {}
        for row in manifest:
            rel = str(row["path"])
            current_scopes = _memberships_for_path(vault_root, vault_root / rel, current)
            prospective_scopes = _memberships_for_path(
                vault_root, vault_root / rel, prospective
            )
            for audience in audiences:
                for purpose in purposes:
                    key = f"{audience}:{purpose or '-'}:{rel}"
                    before[key] = decisions.decide(
                        current_scopes,
                        audience=audience,
                        purpose=purpose,
                        policy=current,
                    ).level
                    after[key] = decisions.decide(
                        prospective_scopes,
                        audience=audience,
                        purpose=purpose,
                        policy=prospective,
                    ).level
    except (GovernanceError, OSError, TypeError, ValueError):
        return "widening"
    return classify_transition_direction(before, after)


def _release_commit_reservation(
    vault_root: Path,
    *,
    proposal_id: str,
    prior_attempt: int,
    attempt_no: int,
    attempt_nonce: str,
    event_id: str,
) -> None:
    """Compensate a reservation when no commit journal can own it."""
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE governance_proposals SET attempt_no=?, attempt_nonce=NULL, "
            "reserved_event_id=NULL WHERE proposal_id=? AND status='pending' "
            "AND attempt_no=? AND attempt_nonce=? AND reserved_event_id=?",
            (
                prior_attempt,
                proposal_id,
                attempt_no,
                attempt_nonce,
                event_id,
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _proposal_guard_value(fingerprint: str, manifest: str) -> dict[str, str]:
    try:
        parsed_manifest = json.loads(manifest)
    except (TypeError, ValueError) as exc:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL", "membership manifest is malformed"
        ) from exc
    if _canonical_json(parsed_manifest) != manifest:
        raise GovernanceError(
            "INVALID_GOVERNANCE_PROPOSAL", "membership manifest is not canonical"
        )
    return {
        "fingerprint_at_propose": fingerprint,
        "membership_manifest_digest": _digest(parsed_manifest),
    }


def _prepare_commit_attempt(
    vault_root: Path,
    proposal_id: str,
    who: RequestPrincipal,
    *,
    direction: str,
    now: float,
    crash_at: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, str], list[str]]:
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, "
            "expires_at, attempt_no, reserved_event_id, created_at, spent_at "
            "FROM governance_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise GovernanceError("PROPOSAL_UNKNOWN", "no such proposal")
    proposal_json, fingerprint, manifest, status, expires_at, prior_attempt, reserved, created_at, spent_at = row
    payload = _validate_proposal_values(
        vault_root,
        proposal_json=str(proposal_json),
        fingerprint=str(fingerprint),
        manifest=str(manifest),
        status=str(status),
        expires_at=float(expires_at),
        now=now,
    )
    if reserved:
        raise GovernanceError("PROPOSAL_RESERVED", "proposal has an open attempt")
    documents = dict(payload["documents"])
    attempt_no = int(prior_attempt) + 1
    attempt_nonce = uuid.uuid4().hex
    root = policy_module.governance_root(vault_root)
    _marker_path(vault_root)
    targets = {rel: policy_target(root, rel) for rel in documents}
    prior_hashes = {rel: _content_hash(targets[rel]) for rel in documents}
    prior_bytes_by_rel: dict[str, bytes | None] = {}
    for rel in documents:
        try:
            prior_bytes_by_rel[rel] = targets[rel].read_bytes()
        except FileNotFoundError:
            prior_bytes_by_rel[rel] = None
    target_hashes = {
        rel: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for rel, content in documents.items()
    }
    prior_components = [
        _component("yaml", rel, {"hash": prior_hashes[rel]}, status="prior")
        for rel in documents
    ]
    prepared_components = [
        _component("yaml", rel, {"hash": target_hashes[rel]}, status="prepared")
        for rel in documents
    ]
    final_components = [
        _component("yaml", rel, {"hash": target_hashes[rel]}, status="active")
        for rel in documents
    ]
    archive_ids = {
        rel: hashlib.sha256(f"{attempt_nonce}:{rel}".encode()).hexdigest()
        for rel in documents
    }
    for rel in documents:
        archive_component = _component(
            "archive",
            archive_ids[rel],
            _archive_value(rel, prior_bytes_by_rel[rel], prior_hashes[rel]),
            status="archived",
        )
        prior_components.append(archive_component)
        prepared_components.append(archive_component)
        final_components.append(archive_component)
    reserved_proposal = authorization_row(
        proposal_json=str(proposal_json), fingerprint_at_propose=str(fingerprint),
        membership_manifest=str(manifest), status="pending", expires_at=float(expires_at),
        attempt_no=attempt_no, attempt_nonce=attempt_nonce, reserved_event_id="SELF_EVENT",
        created_at=float(created_at), spent_at=None,
    )
    final_proposal = {**reserved_proposal, "status": "spent", "reserved_event_id": None, "spent_at": now}
    prior_components.append(_component("proposal", proposal_id, reserved_proposal, status="pending"))
    prepared_components.append(_component("proposal", proposal_id, reserved_proposal, status="pending"))
    final_components.append(_component("proposal", proposal_id, final_proposal, status="spent"))
    proposal_guard = _proposal_guard_value(str(fingerprint), str(manifest))
    prepared_components.append(
        _component("proposal_guard", proposal_id, proposal_guard, status="prepared")
    )
    final_components.append(
        _component("proposal_guard", proposal_id, proposal_guard, status="active")
    )
    phases = {
        "prior": prior_components,
        "prepared": prepared_components,
        "final": final_components,
    }
    digests = {name: _composite(name, values) for name, values in phases.items()}
    event_id = _commit_event_id(
        proposal_id, attempt_no, attempt_nonce, digests["prepared"]
    )
    affected = sorted(
        hashlib.sha256(
            f"{component['component_kind']}:{component['component_key']}".encode()
        ).hexdigest()
        for component in prepared_components
    )
    transition = operation_variant("commit")
    journal_operation = transition.journal_operation
    if journal_operation is None:
        raise GovernanceError("GOVERNANCE_BLOCKED", "operation has no journal identity")
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        live = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, "
            "expires_at, attempt_no, reserved_event_id FROM governance_proposals "
            "WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if live is None:
            raise GovernanceError("PROPOSAL_UNKNOWN", "no such proposal")
        live_payload = _validate_proposal_values(
            vault_root,
            proposal_json=str(live[0]),
            fingerprint=str(live[1]),
            manifest=str(live[2]),
            status=str(live[3]),
            expires_at=float(live[4]),
            now=now,
        )
        if live[6] or int(live[5]) != int(prior_attempt) or live_payload != payload:
            raise GovernanceError("PROPOSAL_RESERVED", "proposal changed during commit")
        reserved_row = conn.execute(
            "UPDATE governance_proposals SET attempt_no=?, attempt_nonce=?, "
            "reserved_event_id=? WHERE proposal_id=? AND status='pending' "
            "AND attempt_no=? AND attempt_nonce IS NULL AND reserved_event_id IS NULL",
            (
                attempt_no,
                attempt_nonce,
                event_id,
                proposal_id,
                prior_attempt,
            ),
        )
        if reserved_row.rowcount != 1:
            raise GovernanceError("PROPOSAL_RESERVED", "proposal changed during commit")
        persisted = _create_journal(
            conn,
            event_id=event_id,
            operation=journal_operation,
            who=who,
            authorization_session=None,
            direction=direction,
            phases=phases,
            child_ids=[event_id],
            marker_required=transition.yaml_marker,
            proposal_id=proposal_id,
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
        for rel in sorted(documents):
            conn.execute(
                "INSERT OR IGNORE INTO governance_policy_archives "
                "(archive_id, event_id, path, prior_bytes, prior_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    archive_ids[rel],
                    event_id,
                    rel,
                    prior_bytes_by_rel[rel],
                    prior_hashes[rel],
                    now,
                ),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    if crash_at == "after_reservation":
        raise GovernanceCrash("after_reservation")

    try:
        receipts.begin_event(
            vault_root,
            operation=transition.receipt_event,
            prior=digests["prior"],
            prepared=digests["prepared"],
            target=digests["final"],
            affected_ids=affected,
            event_id=event_id,
        )
    except BaseException:
        raise
    if crash_at == "after_intent_before_journal":
        raise GovernanceCrash("after_intent_before_journal")

    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        live = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, "
            "expires_at, attempt_no, attempt_nonce, reserved_event_id "
            "FROM governance_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if (
            live is None
            or str(live[0]) != str(proposal_json)
            or str(live[1]) != str(fingerprint)
            or str(live[2]) != str(manifest)
            or str(live[3]) != "pending"
            or float(live[4]) != float(expires_at)
            or int(live[5]) != attempt_no
            or str(live[6]) != attempt_nonce
            or str(live[7]) != event_id
        ):
            raise GovernanceError("PROPOSAL_RESERVED", "commit reservation changed")
        child_ids = [event_id]
        conn.execute(
            "INSERT OR IGNORE INTO governance_operation_journals "
            "(event_id, operation, causation_id, authorization_session, principal_id, phase, "
            "direction, prior_digest, prepared_digest, final_digest, affected_ids, "
            "required_child_intents, required_child_terminals, proposal_id, attempt_no, "
            "marker_required, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                transition.journal_operation,
                event_id,
                who.audience_id,
                direction,
                digests["prior"],
                digests["prepared"],
                digests["final"],
                _canonical_json(affected),
                _canonical_json(child_ids),
                _canonical_json([f"{event_id}:committed"]),
                proposal_id,
                attempt_no,
                1 if transition.yaml_marker else 0,
                now,
                now,
            ),
        )
        _insert_components(conn, event_id, phases)
        for rel in sorted(documents):
            conn.execute(
                "INSERT OR IGNORE INTO governance_policy_archives "
                "(archive_id, event_id, path, prior_bytes, prior_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    archive_ids[rel],
                    event_id,
                    rel,
                    prior_bytes_by_rel[rel],
                    prior_hashes[rel],
                    now,
                ),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        try:
            receipts.abort_event(vault_root, event_id, outcome="reservation_refused")
        except receipts.ReceiptError:
            pass
        _release_commit_reservation(
            vault_root,
            proposal_id=proposal_id,
            prior_attempt=int(prior_attempt),
            attempt_no=attempt_no,
            attempt_nonce=attempt_nonce,
            event_id=event_id,
        )
        raise
    finally:
        conn.close()
    try:
        _validate_proposal_drift(vault_root, proposal_id, now=now)
    except GovernanceError as exc:
        if exc.code != "STALE_GOVERNANCE_POLICY":
            raise
        reconciliation = reconcile_governance_operations(vault_root)
        conn = store.open_connection(vault_root)
        try:
            cleaned = conn.execute(
                "SELECT phase FROM governance_operation_journals WHERE event_id=?",
                (event_id,),
            ).fetchone() == ("aborted",)
            released = conn.execute(
                "SELECT reserved_event_id, attempt_nonce FROM governance_proposals "
                "WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone() == (None, None)
        finally:
            conn.close()
        if reconciliation["blocked"] or not cleaned or not released:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED", "stale commit intent could not be cleaned exactly"
            ) from exc
        raise
    _arm_journal(vault_root, event_id, now=now)
    return event_id, payload, digests, affected


def _proposal_matches_exact_prior(
    vault_root: Path,
    *,
    proposal_json: str,
    fingerprint: str,
    manifest: str,
) -> bool:
    """Classify an orphan reservation only when its exact prior still holds."""
    try:
        payload = json.loads(proposal_json)
        documents = dict(payload["documents"])
        current_policy = policy_module.load(vault_root)
        if current_policy.blocked:
            return False
        prospective = _prospective_policy(vault_root, documents)
        if prospective.blocked:
            return False
        current_manifest = _membership_manifest(
            vault_root, current_policy, prospective, set(documents)
        )
        return (
            current_policy.fingerprint == fingerprint
            and _canonical_json(current_manifest) == manifest
        )
    except (GovernanceError, KeyError, TypeError, ValueError):
        return False


def _validate_proposal_values(
    vault_root: Path,
    *,
    proposal_json: str,
    fingerprint: str,
    manifest: str,
    status: str,
    expires_at: float,
    now: float,
) -> dict[str, Any]:
    if status == "spent":
        raise GovernanceError("PROPOSAL_SPENT", "proposal was already activated")
    if expires_at < now:
        raise GovernanceError("PROPOSAL_EXPIRED", "proposal expired")
    payload = json.loads(proposal_json)
    current_policy = policy_module.load(vault_root)
    if current_policy.blocked:
        raise GovernanceError("GOVERNANCE_BLOCKED", "current policy cannot be evaluated")
    documents = dict(payload["documents"])
    try:
        prospective = _prospective_policy(vault_root, documents)
    except GovernanceError as exc:
        if exc.code != "GOVERNANCE_AUTHORING_UNSTABLE":
            raise
        raise GovernanceError(
            "INVALID_GOVERNANCE_TARGET",
            "the policy workspace cannot be acquired through stable regular-file identities",
        ) from exc
    if prospective.blocked:
        raise GovernanceError(
            "INVALID_GOVERNANCE_POLICY", _canonical_json(list(prospective.findings))
        )
    current_manifest = _membership_manifest(
        vault_root, current_policy, prospective, set(documents)
    )
    if current_policy.fingerprint != fingerprint or _canonical_json(current_manifest) != manifest:
        raise GovernanceError(
            "STALE_GOVERNANCE_POLICY", "policy or exact affected membership changed"
        )
    return payload


def _validate_proposal_drift(vault_root: Path, proposal_id: str, *, now: float) -> None:
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, expires_at "
            "FROM governance_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise GovernanceError("PROPOSAL_UNKNOWN", "no such proposal")
    _validate_proposal_values(
        vault_root,
        proposal_json=str(row[0]),
        fingerprint=str(row[1]),
        manifest=str(row[2]),
        status=str(row[3]),
        expires_at=float(row[4]),
        now=now,
    )


def _commit(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who = _require_owner(kwargs.get("principal"))
    proposal_id = str(kwargs.get("proposal_id") or "")
    if not proposal_id:
        raise GovernanceError("PROPOSAL_UNKNOWN", "proposal_id is required")
    reconcile = reconcile_governance_operations(vault_root)
    if reconcile["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    store.require_authoring_schema(vault_root)
    now = float(kwargs.get("now", time.time()))
    _validate_proposal_drift(vault_root, proposal_id, now=now)
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT proposal_json FROM governance_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise GovernanceError("PROPOSAL_UNKNOWN", "no such proposal")
    proposed_documents = json.loads(str(row[0]))["documents"]
    direction = _effective_transition_direction(vault_root, proposed_documents)
    event_id, payload, digests, affected = _prepare_commit_attempt(
        vault_root,
        proposal_id,
        who,
        direction=direction,
        now=now,
        crash_at=kwargs.get("crash_at"),
    )
    transition = operation_variant("commit")
    if kwargs.get("crash_at") == "after_intent":
        raise GovernanceCrash("after_intent")

    if transition.yaml_marker:
        marker = _marker_path(vault_root)
        _durable_json(
            marker,
            {
                "protocol_version": 1,
                "phase": "pending",
                "schema_version": store.SCHEMA_USER_VERSION,
                "event_id": event_id,
                "operation": transition.journal_operation,
                "prior": digests["prior"],
                "prepared": digests["prepared"],
                "final": digests["final"],
                "affected_paths": sorted(payload["documents"]),
                "affected_ids": sorted(affected),
            },
        )
        if kwargs.get("crash_at") == "after_marker":
            raise GovernanceCrash("after_marker")
    root = policy_module.governance_root(vault_root)
    for index, (rel, content) in enumerate(sorted(payload["documents"].items()), start=1):
        _durable_bytes(policy_target(root, rel), content.encode("utf-8"))
        if kwargs.get("crash_at") in {"after_target_write", f"after_target_write:{index}"}:
            raise GovernanceCrash("after_target_write")
    if kwargs.get("crash_at") == "after_prepare":
        raise GovernanceCrash("after_prepare")
    receipts.commit_event(vault_root, event_id, outcome="prepared")
    if kwargs.get("crash_at") == "after_terminal":
        raise GovernanceCrash("after_terminal")
    if kwargs.get("crash_at") == "after_marker_removal":
        if transition.yaml_marker:
            _remove_marker(vault_root, event_id)
        raise GovernanceCrash("after_marker_removal")
    _activate_event(
        vault_root, event_id, remove_marker=transition.yaml_marker, now=now
    )
    return {
        "status": "committed",
        "event_id": event_id,
        "proposal_id": proposal_id,
        "direction": direction,
    }


def _backfill_plan(vault_root: Path, value: object) -> companion_backfill.BackfillPlan:
    try:
        return companion_backfill.plan(vault_root, value)
    except companion_backfill.CompanionBackfillError as error:
        raise GovernanceError(error.code, error.reason) from error


def _backfill_payload(
    plan: companion_backfill.BackfillPlan,
) -> dict[str, Any]:
    return {
        "kind": "companion-backfill/v1",
        "input": plan.normalized_input,
        "descriptor": plan.descriptor,
        "identities": list(plan.identities),
        "prior": plan.prior_value,
        "target": plan.target_value,
    }


def _backfill_preview(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    _require_owner(kwargs.get("principal"))
    store.require_authoring_schema(vault_root)
    plan = _backfill_plan(vault_root, kwargs.get("companion_input"))
    payload = _backfill_payload(plan)
    proposal_id = uuid.uuid4().hex
    now = float(kwargs.get("now", time.time()))
    expires_at = now + max(
        1, int(kwargs.get("ttl_seconds", DEFAULT_PROPOSAL_TTL_SECONDS))
    )
    payload_json = _canonical_json(payload)
    conn = store.open_connection(vault_root)
    try:
        conn.execute(
            "INSERT INTO governance_proposals "
            "(proposal_id, created_at, expires_at, proposal_json, fingerprint_at_propose, "
            "membership_manifest, status) VALUES (?, ?, ?, ?, ?, '[]', 'pending')",
            (proposal_id, now, expires_at, payload_json, _digest(payload)),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "preview",
        "proposal_id": proposal_id,
        "expires_at": expires_at,
        "descriptor": plan.descriptor,
        "identities": list(plan.identities),
    }


def _backfill_proposal(vault_root: Path, proposal_id: str) -> tuple[Any, ...]:
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, "
            "expires_at, attempt_no, reserved_event_id, created_at, spent_at "
            "FROM governance_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise GovernanceError("PROPOSAL_UNKNOWN", "no such companion backfill proposal")
    return tuple(row)


def _validated_backfill_payload(
    row: tuple[Any, ...], supplied: object
) -> dict[str, Any]:
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError) as error:
        raise GovernanceError(
            "INVALID_COMPANION_BACKFILL", "stored backfill proposal is malformed"
        ) from error
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "companion-backfill/v1"
        or _canonical_json(payload) != str(row[0])
        or _digest(payload) != str(row[1])
        or str(row[2]) != "[]"
        or set(payload) != {
            "kind",
            "input",
            "descriptor",
            "identities",
            "prior",
            "target",
        }
    ):
        raise GovernanceError(
            "INVALID_COMPANION_BACKFILL", "stored backfill proposal is invalid"
        )
    try:
        supplied_json = _canonical_json(supplied)
    except (TypeError, ValueError) as error:
        raise GovernanceError(
            "INVALID_COMPANION_BACKFILL", "companion_input must be exact JSON"
        ) from error
    if supplied_json != _canonical_json(payload["input"]):
        raise GovernanceError(
            "STALE_COMPANION_BACKFILL", "reviewed companion input changed"
        )
    return payload


def _backfill_terminal(
    vault_root: Path, proposal_id: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    conn = store.open_connection(vault_root)
    try:
        journal = conn.execute(
            "SELECT event_id, phase FROM governance_operation_journals "
            "WHERE operation='commit_backfill_companion' AND proposal_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        if journal is None or str(journal[1]) != "closed":
            raise GovernanceError(
                "GOVERNANCE_BLOCKED", "committed companion backfill is incomplete"
            )
        current = _actual_backfill_value(
            vault_root, str(payload["input"]["expected_companion_path"])
        )
        if current != payload["target"]:
            raise GovernanceError(
                "STALE_COMPANION_BACKFILL", "committed companion bytes changed"
            )
    finally:
        conn.close()
    return {
        "status": "committed",
        "event_id": str(journal[0]),
        "proposal_id": proposal_id,
        "direction": "widening",
    }


def _actual_backfill_value(vault_root: Path, companion_path: str) -> dict[str, Any]:
    try:
        snapshot = reserved_paths.read_generic_bytes(vault_root, companion_path)
    except reserved_paths.ReservedPathLeafError as error:
        raise GovernanceError(
            "STALE_COMPANION_BACKFILL", "companion snapshot is unavailable"
        ) from error
    return {
        "path_hash": hashlib.sha256(companion_path.encode("utf-8")).hexdigest(),
        "sha256": hashlib.sha256(snapshot.data).hexdigest(),
        "size": len(snapshot.data),
    }


def _backfill_commit(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who = _require_owner(kwargs.get("principal"))
    proposal_id = str(kwargs.get("proposal_id") or "")
    if not proposal_id:
        raise GovernanceError("PROPOSAL_UNKNOWN", "proposal_id is required")
    reconciliation = reconcile_governance_operations(vault_root)
    if reconciliation["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs repair")
    store.require_authoring_schema(vault_root)
    now = float(kwargs.get("now", time.time()))
    row = _backfill_proposal(vault_root, proposal_id)
    payload = _validated_backfill_payload(row, kwargs.get("companion_input"))
    status = str(row[3])
    if status == "spent":
        return _backfill_terminal(vault_root, proposal_id, payload)
    if status != "pending" or float(row[4]) < now:
        raise GovernanceError("PROPOSAL_EXPIRED", "backfill proposal is not active")
    if row[6] is not None:
        raise GovernanceError("PROPOSAL_RESERVED", "backfill proposal has an open attempt")
    plan = _backfill_plan(vault_root, kwargs.get("companion_input"))
    if _backfill_payload(plan) != payload:
        raise GovernanceError(
            "STALE_COMPANION_BACKFILL", "reviewed companion snapshots changed"
        )

    prior_attempt = int(row[5])
    attempt_no = prior_attempt + 1
    attempt_nonce = uuid.uuid4().hex
    reserved_proposal = authorization_row(
        proposal_json=str(row[0]),
        fingerprint_at_propose=str(row[1]),
        membership_manifest=str(row[2]),
        status="pending",
        expires_at=float(row[4]),
        attempt_no=attempt_no,
        attempt_nonce=attempt_nonce,
        reserved_event_id="SELF_EVENT",
        created_at=float(row[7]),
        spent_at=None,
    )
    final_proposal = {
        **reserved_proposal,
        "status": "spent",
        "reserved_event_id": None,
        "spent_at": now,
    }
    phases = {
        "prior": [
            _component("companion", plan.companion_path, plan.prior_value, status="prior"),
            _component("proposal", proposal_id, reserved_proposal, status="pending"),
        ],
        "prepared": [
            _component(
                "companion", plan.companion_path, plan.target_value, status="prepared"
            ),
            _component("proposal", proposal_id, reserved_proposal, status="pending"),
        ],
        "final": [
            _component("companion", plan.companion_path, plan.target_value, status="active"),
            _component("proposal", proposal_id, final_proposal, status="spent"),
        ],
    }
    digests = {phase: _composite(phase, values) for phase, values in phases.items()}
    event_id = receipts.critical_event_id(
        {
            "operation": "governance_companion_backfill",
            "proposal_id": proposal_id,
            "attempt": attempt_no,
            "attempt_nonce": attempt_nonce,
            "prepared": digests["prepared"],
        }
    )
    affected = sorted(
        {
            hashlib.sha256(
                f"{item['component_kind']}:{item['component_key']}".encode()
            ).hexdigest()
            for item in phases["prepared"]
        }
    )
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        live = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, "
            "expires_at, attempt_no, reserved_event_id FROM governance_proposals "
            "WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if (
            live is None
            or tuple(live[:5]) != tuple(row[:5])
            or int(live[5]) != prior_attempt
            or live[6] is not None
        ):
            raise GovernanceError("PROPOSAL_RESERVED", "backfill proposal changed")
        updated = conn.execute(
            "UPDATE governance_proposals SET attempt_no=?, attempt_nonce=?, "
            "reserved_event_id=? WHERE proposal_id=? AND status='pending' "
            "AND attempt_no=? AND attempt_nonce IS NULL AND reserved_event_id IS NULL",
            (attempt_no, attempt_nonce, event_id, proposal_id, prior_attempt),
        )
        if updated.rowcount != 1:
            raise GovernanceError("PROPOSAL_RESERVED", "backfill proposal changed")
        persisted = _create_journal(
            conn,
            event_id=event_id,
            operation="commit_backfill_companion",
            who=who,
            authorization_session=None,
            direction="widening",
            phases=phases,
            child_ids=[event_id],
            proposal_id=proposal_id,
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "backfill digest changed")
        conn.execute(
            "UPDATE governance_operation_journals SET attempt_no=? WHERE event_id=?",
            (attempt_no, event_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    if kwargs.get("crash_at") == "after_reservation":
        raise GovernanceCrash("after_reservation")

    receipts.begin_event(
        vault_root,
        operation="governance_companion_backfill",
        prior=digests["prior"],
        prepared=digests["prepared"],
        target=digests["final"],
        affected_ids=affected,
        event_id=event_id,
    )
    if kwargs.get("crash_at") == "after_intent":
        raise GovernanceCrash("after_intent")
    try:
        confirmed = _backfill_plan(vault_root, kwargs.get("companion_input"))
        if _backfill_payload(confirmed) != payload:
            raise GovernanceError(
                "STALE_COMPANION_BACKFILL", "companion changed after receipt intent"
            )
    except GovernanceError:
        reconcile_governance_operations(vault_root)
        raise
    _arm_journal(vault_root, event_id, now=now)
    if kwargs.get("crash_at") == "after_arming":
        raise GovernanceCrash("after_arming")
    try:
        reserved_paths.publish_generic_bytes(
            vault_root,
            plan.companion_path,
            plan.target_bytes,
            expected_identity=plan.companion_identity,
            expected_sha256=hashlib.sha256(plan.prior_bytes).hexdigest(),
        )
    except reserved_paths.ReservedPathLeafError as error:
        reconcile_governance_operations(vault_root)
        raise GovernanceError(
            "STALE_COMPANION_BACKFILL", "companion changed during publication"
        ) from error
    if kwargs.get("crash_at") == "after_publish":
        raise GovernanceCrash("after_publish")
    receipts.commit_event(vault_root, event_id, outcome="prepared")
    if kwargs.get("crash_at") == "after_terminal":
        raise GovernanceCrash("after_terminal")
    _activate_event(vault_root, event_id, remove_marker=False, now=now)
    return {
        "status": "committed",
        "event_id": event_id,
        "proposal_id": proposal_id,
        "direction": "widening",
    }


def _durable_remove(path: Path) -> None:
    if path.exists():
        path.unlink()
        _fsync_directory(path.parent)


def _yaml_transition(
    vault_root: Path,
    *,
    transition: OperationVariant,
    who: RequestPrincipal,
    documents: Mapping[str, str | None],
    direction: str,
    dependent_targets: Mapping[str, str] | None = None,
    source_event_id: str | None = None,
    crash_at: str | None = None,
    now: float,
) -> dict[str, Any]:
    """Apply a pre-resolved YAML/sidecar target without semantic replay."""
    operation = transition.journal_operation
    if operation is None:
        raise GovernanceError("GOVERNANCE_BLOCKED", "operation has no journal identity")
    if not documents:
        raise GovernanceError("NO_GOVERNANCE_CHANGE", "no policy documents would change")
    root = policy_module.governance_root(vault_root)
    _marker_path(vault_root)
    targets = {rel: policy_target(root, rel) for rel in documents}
    prior_hashes = {rel: _content_hash(targets[rel]) for rel in documents}
    prior_bytes_by_rel: dict[str, bytes | None] = {}
    for rel in documents:
        try:
            prior_bytes_by_rel[rel] = targets[rel].read_bytes()
        except FileNotFoundError:
            prior_bytes_by_rel[rel] = None
    target_hashes = {
        rel: (
            "absent"
            if content is None
            else hashlib.sha256(content.encode("utf-8")).hexdigest()
        )
        for rel, content in documents.items()
    }
    event_id = receipts.critical_event_id(
        {
            "operation": operation,
            "target_hashes": target_hashes,
            "nonce": uuid.uuid4().hex,
        }
    )
    phases: dict[str, list[dict[str, Any]]] = {
        "prior": [
            _component("yaml", rel, {"hash": prior_hashes[rel]}, status="prior")
            for rel in documents
        ],
        "prepared": [
            _component("yaml", rel, {"hash": target_hashes[rel]}, status="prepared")
            for rel in documents
        ],
        "final": [
            _component("yaml", rel, {"hash": target_hashes[rel]}, status="active")
            for rel in documents
        ],
    }
    archive_ids = {
        rel: hashlib.sha256(f"{event_id}:{rel}".encode()).hexdigest()
        for rel in documents
    }
    for rel in documents:
        archive_component = _component(
            "archive",
            archive_ids[rel],
            _archive_value(rel, prior_bytes_by_rel[rel], prior_hashes[rel]),
            status="archived",
        )
        for phase in phases.values():
            phase.append(archive_component)
    dependent_targets = dict(dependent_targets or {})
    if dependent_targets:
        conn = store.open_connection(vault_root)
        try:
            rows = {
                str(row[0]): row[1:]
                for row in conn.execute(
                    "SELECT grant_id, authorization_session, audience, purpose, ceiling, paths, "
                    "fingerprints, token_jti, status, prepared_event_id, created_at, expires_at, "
                    "revoked_at, membership_manifest, policy_fingerprint FROM "
                    "governance_session_grants WHERE grant_id IN "
                    f"({','.join('?' for _ in dependent_targets)})",
                    tuple(sorted(dependent_targets)),
                )
            }
        finally:
            conn.close()
        if set(rows) != set(dependent_targets):
            raise GovernanceError("STALE_DEPENDENT_GRANT", "a dependent grant disappeared")
        for grant_id in sorted(dependent_targets):
            row = rows[grant_id]
            prior = _grant_projection(
                authorization_session=str(row[0]), audience=str(row[1]), purpose=row[2],
                ceiling=int(row[3]), paths=str(row[4]), fingerprints=str(row[5]),
                token_jti=str(row[6]), status=str(row[7]), prepared_event_id=row[8],
                created_at=float(row[9]), expires_at=float(row[10]), revoked_at=row[11],
                membership_manifest=str(row[12]), policy_fingerprint=str(row[13]),
            )
            phases["prior"].append(
                _component(
                    "dependent_grant",
                    grant_id,
                    prior,
                    status=str(row[7]),
                )
            )
            phases["prepared"].append(
                _component(
                    "dependent_grant",
                    grant_id,
                    {**prior, "status": "prepared_undo", "prepared_event_id": event_id},
                    status="prepared_undo",
                )
            )
            phases["final"].append(
                _component(
                    "dependent_grant",
                    grant_id,
                    {**prior, "status": dependent_targets[grant_id], "prepared_event_id": None},
                    status=dependent_targets[grant_id],
                )
            )

    digests = {phase: _composite(phase, rows) for phase, rows in phases.items()}
    affected_ids = sorted(
        hashlib.sha256(f"{row['component_kind']}:{row['component_key']}".encode()).hexdigest()
        for row in phases["prepared"]
    )
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        persisted = _create_journal(
            conn,
            event_id=event_id,
            operation=operation,
            who=who,
            authorization_session=None,
            direction=direction,
            phases=phases,
            child_ids=[event_id],
            marker_required=transition.yaml_marker,
            proposal_id=(f"undo:{source_event_id}" if source_event_id else None),
            phase="allocating",
            now=now,
        )
        if persisted != digests:
            raise GovernanceError("GOVERNANCE_BLOCKED", "transition digest changed")
        for rel in sorted(documents):
            conn.execute(
                "INSERT INTO governance_policy_archives "
                "(archive_id, event_id, path, prior_bytes, prior_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    archive_ids[rel],
                    event_id,
                    rel,
                    prior_bytes_by_rel[rel],
                    prior_hashes[rel],
                    now,
                ),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    receipts.begin_event(
        vault_root,
        operation=transition.receipt_event,
        prior=digests["prior"],
        prepared=digests["prepared"],
        target=digests["final"],
        affected_ids=affected_ids,
        event_id=event_id,
    )
    _arm_journal(vault_root, event_id, now=now)
    if crash_at == "after_intent":
        raise GovernanceCrash("after_intent")
    if transition.yaml_marker:
        _durable_json(
            _marker_path(vault_root),
            {
                "protocol_version": 1,
                "phase": "pending",
                "schema_version": store.SCHEMA_USER_VERSION,
                "event_id": event_id,
                "operation": operation,
                "prior": digests["prior"],
                "prepared": digests["prepared"],
                "final": digests["final"],
                "affected_paths": sorted(documents),
                "affected_ids": affected_ids,
            },
        )
        if crash_at == "after_marker":
            raise GovernanceCrash("after_marker")
    for index, (rel, content) in enumerate(sorted(documents.items()), start=1):
        target = policy_target(root, rel)
        if content is None:
            _durable_remove(target)
        else:
            _durable_bytes(target, content.encode("utf-8"))
        if crash_at in {"after_target_write", f"after_target_write:{index}"}:
            raise GovernanceCrash("after_target_write")
    if dependent_targets:
        conn = store.open_connection(vault_root)
        try:
            conn.execute(
                "UPDATE governance_session_grants SET status='prepared_undo', "
                "prepared_event_id=? WHERE grant_id IN "
                f"({','.join('?' for _ in dependent_targets)})",
                (event_id, *sorted(dependent_targets)),
            )
            conn.commit()
        finally:
            conn.close()
    if crash_at == "after_prepare":
        raise GovernanceCrash("after_prepare")
    receipts.commit_event(vault_root, event_id, outcome="prepared")
    if crash_at == "after_terminal":
        raise GovernanceCrash("after_terminal")
    _activate_event(
        vault_root, event_id, remove_marker=transition.yaml_marker, now=now
    )
    return {
        "status": "committed",
        "event_id": event_id,
        "operation": operation,
        "direction": direction,
    }


def _toggle_rules(vault_root: Path, operation: str, **kwargs: Any) -> dict[str, Any]:
    who = _require_owner(kwargs.get("principal"))
    reconciliation = reconcile_governance_operations(vault_root)
    if reconciliation["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    store.require_authoring_schema(vault_root)
    raw_ids = kwargs.get("rule_ids")
    if not isinstance(raw_ids, list) or not raw_ids or not all(
        isinstance(rule_id, str) and rule_id for rule_id in raw_ids
    ):
        raise GovernanceError("INVALID_RULE_SET", "rule_ids must be a non-empty list")
    rule_ids = set(raw_ids)
    current = policy_module.load(vault_root)
    selected = [rule for rule in current.rules if rule.id in rule_ids]
    if {rule.id for rule in selected} != rule_ids:
        raise GovernanceError("RULE_UNKNOWN", "one or more rules do not exist")
    documents: dict[str, str | None] = {}
    for source in sorted({rule.source for rule in selected}):
        path = policy_module.governance_root(vault_root) / source
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise GovernanceError("INVALID_POLICY_DOCUMENT", f"{source} is not a mapping")
        options = data.get("options")
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise GovernanceError("INVALID_POLICY_DOCUMENT", f"{source}:options is invalid")
        rule_id = str(data.get("id") or "")
        if rule_id in rule_ids:
            if operation == "suspend":
                options["suspended"] = True
            else:
                options.pop("suspended", None)
        if options:
            data["options"] = options
        else:
            data.pop("options", None)
        documents[source] = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    direction = _effective_transition_direction(vault_root, documents)
    return _yaml_transition(
        vault_root,
        transition=operation_variant(operation),
        who=who,
        documents=documents,
        direction=direction,
        crash_at=kwargs.get("crash_at"),
        now=float(kwargs.get("now", time.time())),
    )


def _undo(vault_root: Path, **kwargs: Any) -> dict[str, Any]:
    who = _require_owner(kwargs.get("principal"))
    reconciliation = reconcile_governance_operations(vault_root)
    if reconciliation["blocked"]:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending operation needs manual repair")
    store.require_authoring_schema(vault_root)
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT event_id FROM governance_operation_journals j "
            "WHERE phase='closed' AND operation IN "
            "('commit','suspend','resume','standing_grant','standing_revoke') "
            "AND NOT EXISTS (SELECT 1 FROM governance_operation_journals u "
            "WHERE u.operation='undo' AND u.proposal_id='undo:' || j.event_id "
            "AND u.phase='closed') ORDER BY updated_at DESC, event_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise GovernanceError("NOTHING_TO_UNDO", "no archived policy change is available")
        source_event_id = str(row[0])
        archive_rows = conn.execute(
            "SELECT path, prior_bytes FROM governance_policy_archives "
            "WHERE event_id=? ORDER BY path",
            (source_event_id,),
        ).fetchall()
        grant_rows = conn.execute(
            "SELECT grant_id, paths, fingerprints, membership_manifest "
            "FROM governance_session_grants "
            "WHERE status='active' ORDER BY grant_id"
        ).fetchall()
    finally:
        conn.close()
    if not archive_rows:
        raise GovernanceError("ARCHIVE_MISSING", "the prior policy archive is unavailable")
    documents = {
        str(path): (None if prior_bytes is None else bytes(prior_bytes).decode("utf-8"))
        for path, prior_bytes in archive_rows
    }
    restored_policy = _prospective_policy(vault_root, documents)
    if restored_policy.blocked:
        raise GovernanceError("ARCHIVE_INVALID", "the restored policy does not compile")
    dependent_targets: dict[str, str] = {}
    for grant_id, raw_paths, raw_fingerprints, raw_membership in grant_rows:
        paths = [str(path) for path in json.loads(str(raw_paths))]
        fingerprints = [str(value) for value in json.loads(str(raw_fingerprints))]
        current = [_content_hash(vault_root / path) for path in paths]
        restored_membership = _resolved_membership_manifest(
            vault_root, restored_policy, paths
        )
        dependent_targets[str(grant_id)] = (
            "active"
            if current == fingerprints
            and restored_membership == json.loads(str(raw_membership))
            else "expired"
        )
    direction = _effective_transition_direction(vault_root, documents)
    return _yaml_transition(
        vault_root,
        transition=operation_variant("undo"),
        who=who,
        documents=documents,
        direction=direction,
        dependent_targets=dependent_targets,
        source_event_id=source_event_id,
        crash_at=kwargs.get("crash_at"),
        now=float(kwargs.get("now", time.time())),
    )


from .recovery import (  # noqa: E402 - imported after circular protocol helpers exist
    _activate_event,
    _remove_marker,
    reconcile_governance_operations,
)


def _selected_variant(
    operation: str, _spec: OperationSpec, kwargs: Mapping[str, Any]
) -> OperationVariant:
    if operation == "backfill_companion":
        action = kwargs.get("backfill_action")
        if action not in {"preview", "commit"}:
            raise GovernanceError(
                "INVALID_COMPANION_BACKFILL",
                "backfill_action must be preview or commit",
            )
        return select_operation(operation, "commit" if action == "commit" else None)
    scope = kwargs.get("scope")
    if operation == "grant" and scope not in (None, "session", "standing"):
        raise GovernanceError(
            "INVALID_GRANT_SCOPE", "scope must be omitted, session, or standing"
        )
    return select_operation(operation, scope)


def _inspect(vault_root: Path, operation: str, **kwargs: Any) -> dict[str, Any]:
    from .inspection import InspectionError, inspect_operation

    try:
        return inspect_operation(vault_root, operation, **kwargs)
    except InspectionError as exc:
        raise GovernanceError(exc.code, exc.reason) from exc


def _not_implemented(_vault_root: Path, operation: str, **_kwargs: Any) -> dict[str, Any]:
    raise GovernanceError("GOVERNANCE_OPERATION_UNAVAILABLE", f"{operation} is not implemented")


_HANDLER_STRATEGIES: Mapping[str, Any] = MappingProxyType(
    {
        "backfill_companion_commit": _backfill_commit,
        "backfill_companion_preview": _backfill_preview,
        "inspect": _inspect,
        "proposal": _proposal,
        "commit": _commit,
        "grant_session": _grant,
        "grant_standing": _standing_grant,
        "revoke_session": _revoke,
        "revoke_standing": _standing_revoke,
        "toggle_rules": _toggle_rules,
        "undo": _undo,
        "declare": _declare,
    }
)
if frozenset(_HANDLER_STRATEGIES) != HANDLER_STRATEGY_KEYS:
    raise RuntimeError("governance handler strategies do not cover registry keys")


def op_govern_memory(vault_root: Path, operation: str, **kwargs: Any) -> dict[str, Any]:
    """Inspect or author opt-in confidential governance policy."""
    # This lookup is deliberately the first executable action.  Unknown input
    # must not create a sidecar, policy directory, receipt, or marker.
    spec = OPERATION_SPECS.get(operation)
    if spec is None:
        raise GovernanceError(
            "UNKNOWN_GOVERNANCE_OPERATION", f"unsupported operation {operation!r}"
        )
    if not reserved_paths.owner_authorized("governance-tree"):
        raise GovernanceError(
            "GOVERNANCE_AUTHORITY_REQUIRED",
            "governance operations require dispatcher-issued owner authority",
        )
    root = Path(vault_root)
    selection = _selected_variant(operation, spec, kwargs)
    if not spec.read_only:
        _authorize_operation(selection, kwargs)
        # One registry-driven gate for every authoring operation, rather than
        # thirteen `.blocked` sites. Prospective policy paths independently
        # re-probe the conflict set around their held-handle byte acquisition;
        # this fast gate also covers authoring variants that do not compile.
        if policy_module.has_conflict_copy(root):
            raise GovernanceError(
                "GOVERNANCE_CONFLICTED",
                "a synchronisation conflict copy is present under _Governance/; "
                "resolve it before authoring policy",
            )
    handler = _HANDLER_STRATEGIES.get(selection.handler_key)
    if not callable(handler):
        raise GovernanceError(
            "GOVERNANCE_OPERATION_UNAVAILABLE", f"{operation} has no handler"
        )
    result = handler(root, operation=operation, _selection=selection, **kwargs)
    if not spec.read_only:
        from .. import writer_lease

        writer_lease.mark_active_mutation_committed()
    return result
