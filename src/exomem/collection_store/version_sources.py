"""Authenticated Source membership of immutable collection payload versions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from .. import memory_refs, record_source_custody, source_capture
from . import tables


def _reference(value: str) -> str:
    identity = memory_refs.parse_memory_ref(value)
    if identity is None or memory_refs.memory_ref(identity) != value:
        raise ValueError("Source association requires a canonical incarnation")
    return value


class Binding(BaseModel):
    """Logical event material; physical row IDs never supply cross-store identity."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    effect_ordinal: Annotated[int, Field(ge=0)]
    item_key: Annotated[str, Field(min_length=1)]
    row_version: Annotated[int, Field(ge=1)]
    payload_hash: Annotated[str, AfterValidator(source_capture.validate_sha256)]
    # The existing record transition protocol fixes this identifier's format.
    creation_transition: Annotated[str, Field(pattern=r"^[0-9a-f]{24}$")]
    canonical_ref: Annotated[str, AfterValidator(_reference)]
    dep_epoch: Annotated[str, Field(min_length=1, max_length=1024)] | None
    dep_operation: Annotated[str, Field(min_length=1, max_length=1024)]


@dataclass(frozen=True)
class EffectInput:
    item_key: str
    payload_hash: str
    current: record_source_custody.Facts
    prior: record_source_custody.Facts
    before_row_id: int | None
    before_version: int | None


def effect_input(key: str, payload: str, body: str, before: Mapping | None, *,
                 explicit: Sequence = (), prepared=None) -> EffectInput:
    """Reuse the leaf's resolved inputs; never resolve historical presentation paths again."""
    current = record_source_custody.facts(body, prepared=prepared)
    references, complete = list(current.references), current.complete
    for source in explicit:
        facts = record_source_custody.facts(explicit=source)
        references.extend(facts.references)
        complete = complete and facts.complete
    references = tuple(dict.fromkeys(references))
    limit = record_source_custody.MAX_PAYLOAD_REFERENCES
    return EffectInput(key, payload, record_source_custody.Facts(references[:limit], complete and len(references) <= limit),
        record_source_custody.facts(before["body"] if before else None),
        None if before is None else before["row_id"], None if before is None else before["row_version"])


def plan(conn, inputs: Sequence[EffectInput], *, transition: str, operation: str,
         epochs: Mapping[str, str] | None = None) -> tuple[tuple[Binding, ...], list[tuple[int, bool, bool]]]:
    """Bind current and retained owners in one bounded lookup over this operation's prior versions."""
    epochs = epochs or {}
    requested = [(value.before_row_id, value.before_version) for value in inputs if value.before_row_id is not None]
    prior = {}
    if requested:
        rows = conn.execute(
            "WITH requested AS (SELECT json_extract(value,'$[0]') AS row_id, "
            "json_extract(value,'$[1]') AS row_version FROM json_each(?)) "
            "SELECT v.row_id,v.row_version,v.payload_hash,t.transition_id,e.dependency_current_complete,"
            "COALESCE(json_group_array(b.canonical_ref) FILTER (WHERE b.canonical_ref IS NOT NULL),'[]') "
            "FROM requested r JOIN version_identity v ON v.row_id=r.row_id AND v.row_version=r.row_version "
            "JOIN txns t ON t.txn_id=v.txn_id "
            "LEFT JOIN audit_effects e ON e.txn_id=v.txn_id AND e.row_id=v.row_id AND e.version_after=v.row_version "
            "LEFT JOIN version_source_bindings b ON b.row_id=v.row_id AND b.row_version=v.row_version "
            "GROUP BY v.row_id,v.row_version", (json.dumps(requested),)).fetchall()
        prior = {(row_id, version): (payload, creator, complete, json.loads(references))
                 for row_id, version, payload, creator, complete, references in rows}
    bindings, completeness = [], []
    for ordinal, value in enumerate(inputs):
        version = 1 if value.before_version is None else value.before_version + 1
        for reference in value.current.references:
            bindings.append(Binding(effect_ordinal=ordinal, item_key=value.item_key, row_version=version,
                payload_hash=value.payload_hash, creation_transition=transition, canonical_ref=reference,
                dep_epoch=epochs.get(reference), dep_operation=operation))
        prior_complete = True
        if value.before_row_id is not None:
            owner = prior.get((value.before_row_id, value.before_version))
            if owner is None:
                raise ValueError("retained Source inputs have no immutable version owner")
            payload, creator, recorded_complete, references = owner
            # Legacy paths cannot establish which Source incarnation the old version resolved.
            prior_complete = recorded_complete == 1 and value.prior.complete
            for reference in dict.fromkeys((*references, *value.prior.references)):
                bindings.append(Binding(effect_ordinal=ordinal, item_key=value.item_key, row_version=value.before_version,
                    payload_hash=payload, creation_transition=creator, canonical_ref=reference,
                    dep_epoch=epochs.get(reference), dep_operation=operation))
        completeness.append((ordinal, value.current.complete, prior_complete))
    return tuple(bindings), completeness


def material(bindings: Sequence[Binding]) -> list[dict]:
    ordered = sorted(bindings, key=lambda binding: (
        binding.effect_ordinal, binding.item_key, binding.row_version, binding.canonical_ref))
    return [binding.model_dump(mode="json") for binding in ordered]


def existing(conn, collection_id: str, bindings: Sequence[Binding]) -> frozenset[tuple[str, int, str]]:
    """Find exact owners in one indexed batch, without reading other versions or Source bodies."""
    if not bindings:
        return frozenset()
    requested = json.dumps([[binding.item_key, binding.row_version, binding.canonical_ref] for binding in bindings])
    return frozenset(conn.execute(
        "WITH requested AS (SELECT json_extract(value,'$[0]') AS item_key, "
        "json_extract(value,'$[1]') AS row_version,json_extract(value,'$[2]') AS canonical_ref FROM json_each(?)) "
        "SELECT r.item_key,r.row_version,r.canonical_ref FROM requested r "
        # CROSS JOIN keeps the request driving: stores keep no planner statistics, which would scan the collection.
        "CROSS JOIN item_rows i ON i.collection_id=? AND i.item_key=r.item_key "
        "JOIN version_source_bindings b ON b.row_id=i.row_id AND b.row_version=r.row_version "
        "AND b.canonical_ref=r.canonical_ref", (requested, collection_id)))


def novel(conn, collection_id: str, bindings: Sequence[Binding]) -> tuple[Binding, ...]:
    """Deduplicate canonical ownership while leaving ordered presentation citations untouched."""
    owners = {}
    for binding in bindings:
        key = binding.item_key, binding.row_version, binding.canonical_ref
        prior = owners.setdefault(key, binding)
        if prior != binding:
            raise ValueError("one Source association has conflicting producer evidence")
    established = existing(conn, collection_id, tuple(owners.values()))
    return tuple(binding for key, binding in owners.items() if key not in established)


def persist(writer, bindings: Sequence[Binding], *, txn_id: int, row_ids: Mapping[str, int]) -> None:
    """Translate the already planned logical owners through this batch's allocated row IDs."""
    if not bindings:
        return
    writer._execute(tables.INSERT_SOURCE_BINDING, [
        {"row_id": row_ids[binding.item_key], "row_version": binding.row_version,
         "canonical_ref": binding.canonical_ref, "dep_epoch": binding.dep_epoch,
         "dep_operation": binding.dep_operation, "auth_txn_id": txn_id,
         "auth_effect_ordinal": binding.effect_ordinal}
        for binding in bindings])


def reconstruct(conn, txn: Mapping) -> list[Binding]:
    """Reproduce new associations after their effects, under the history verifier's qualified prefix.

    The existing verifier proves each owner's payload at its creation transition. This join
    binds that immutable identity; it does not decode the same payload once per Source.
    A current owner's creation transition may be the pending transaction not inserted yet.
    """
    rows = conn.execute(
        "SELECT b.auth_effect_ordinal,i.collection_id,i.item_key,b.row_version,v.payload_hash,"
        "v.txn_id,creation.transition_id,b.canonical_ref,b.dep_epoch,b.dep_operation,"
        "e.row_id,e.item_key,e.version_before,e.version_after,b.row_id "
        "FROM version_source_bindings b "
        "LEFT JOIN item_rows i ON i.row_id=b.row_id "
        "LEFT JOIN version_identity v ON v.row_id=b.row_id AND v.row_version=b.row_version "
        "LEFT JOIN txns creation ON creation.txn_id=v.txn_id "
        "LEFT JOIN audit_effects e ON e.txn_id=b.auth_txn_id AND e.ordinal=b.auth_effect_ordinal "
        "WHERE b.auth_txn_id=? ORDER BY b.auth_effect_ordinal,i.item_key,b.row_version,b.canonical_ref",
        (txn["txn_id"],)).fetchall()
    bindings = []
    for (ordinal, collection_id, key, version, payload, creator_id, creator, reference, epoch, operation,
         effect_row, effect_key, before, after, row_id) in rows:
        if (collection_id != txn["collection_id"] or effect_row != row_id or effect_key != key
                or version not in (before, after)):
            raise ValueError("Source association differs from its authenticating effect")
        if creator_id == txn["txn_id"]:
            creator = txn["transition_id"]
        bindings.append(Binding(effect_ordinal=ordinal, item_key=key, row_version=version,
            payload_hash=payload, creation_transition=creator, canonical_ref=reference,
            dep_epoch=epoch, dep_operation=operation))
    return bindings


def event_fields(conn, txn: Mapping, *, bindings: Sequence[Binding] | None = None,
                 completeness: Sequence[tuple[int, bool | None, bool | None]] | None = None) -> dict:
    """The v2 extension, reconstructed from the same immutable effect and owner records."""
    store = conn.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()
    if store is None:
        raise ValueError("Source association has no logical store identity")
    if bindings is None:
        bindings = reconstruct(conn, txn)
    if completeness is None:
        completeness = [(ordinal, None if current is None else bool(current), None if prior is None else bool(prior))
                        for ordinal, current, prior in conn.execute(
                            "SELECT ordinal,dependency_current_complete,dependency_prior_complete "
                            "FROM audit_effects WHERE txn_id=? ORDER BY ordinal", (txn["txn_id"],))]
    return {"store_id": store[0], "source_bindings": material(bindings),
            "source_completeness": [{"effect_ordinal": ordinal, "current": current, "prior": prior}
                                    for ordinal, current, prior in completeness]}
