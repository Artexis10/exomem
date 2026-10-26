"""Revert one recent promotion in one call.

An edge or an entity reverts through the curation executor: the call builds the
deterministic revert plan from current state, checks the caller's
`revert_fingerprint` against it, seals it and applies it in the same call. A
registry addition reverts through one `schema_memory` save and does not pass
here. See `promotions.py` for the ledger these functions read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import curation, promotion_proof, promotions


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    return str(error).split(":", 1)[0]


# --------------------------------------------------------------------------- #
# The leaf: remove one typed edge
# --------------------------------------------------------------------------- #

_STALE_RETRIES = 3


def _region_without(lines: list[str], doomed: set[int], body: str) -> tuple[str, str]:
    """The smallest run of whole lines holding every doomed line and occurring once in `body`.

    The edit replaces that run with itself minus the doomed lines, so the rest of
    the page stays byte for byte. A run that occurs twice cannot name one place,
    so it grows by a neighbouring line until it does.
    """
    low, high = min(doomed), max(doomed)
    while True:
        region = "".join(lines[low : high + 1])
        if body.count(region) == 1:
            kept = "".join(
                line for index, line in enumerate(lines[low : high + 1], low) if index not in doomed
            )
            return region, kept
        if low > 0:
            low -= 1
        elif high < len(lines) - 1:
            high += 1
        else:  # the whole body occurs once, so this cannot be reached
            raise ValueError("CURATION_RELATION_ABSENT: the edge cannot be told apart in its page")


def remove_relation(
    vault_root: Path, *, subject: str, predicate: str, target: str, why: str,
    _promotion: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Remove every `## Relations` bullet of one (subject, predicate, target) triple.

    The leaf reads the page fresh and binds the edit to the bytes it just read, so
    it needs no hash from the caller and a concurrent edit of another part of the
    page survives: a stale hash is read again, never forced. A triple that is not
    there reports `absent`, which is also what a repeat sees.
    """
    from . import edit

    if not promotions.page_visible(vault_root, subject):
        raise ValueError(f"NOT_FOUND: file does not exist: {subject}")
    for _attempt in range(_STALE_RETRIES):
        editable = edit.load_editable(vault_root, subject)
        guards = ()
        if _promotion is not None:
            binding = promotion_proof.metadata(_promotion)
            _promotion_id, entry = promotions.find(vault_root, binding["ref"])
            matches = [item for item in entry["subjects"] if (
                entry["kind"] == "edge" and item.get("subject") == subject
                and item.get("predicate") == predicate and item.get("target") == target
            )]
            if len(matches) != 1:
                raise ValueError("PROMOTION_SUBJECT_UNVERIFIED: edge binding unavailable")
            proof = promotion_proof.page(vault_root, "edge", matches[0])
            promotion_proof.require_same([proof])
            if promotion_proof.subject_fingerprint(matches[0]) != binding["expected_fingerprint"]:
                raise ValueError("PROMOTION_SUBJECT_CHANGED: the edge proof changed")
            for guard in proof.guards:
                guard.recheck(vault_root)
            # The editor's expected hash and primary guard own this write target.
            guards = tuple(guard for guard in proof.guards if guard.target != subject)
        body = editable.body
        from . import relation_registry
        from .vault import parse_frontmatter

        registry = relation_registry.load_for_page(vault_root, subject, parse_frontmatter(editable.original_text)[0])
        canonical = registry.resolve(predicate).canonical or predicate
        doomed = {
            index
            for index, kind, resolved in promotions.authored_edges(vault_root, body)
            if (registry.resolve(kind).canonical or kind) == canonical and resolved == target
        }
        if not doomed:
            return {"path": subject, "removed": 0, "outcome": "absent"}
        lines = body.splitlines(keepends=True)
        region, kept = _region_without(lines, doomed, body)
        try:
            result = edit.edit(
                vault_root, path=subject, why=why, old_string=region, new_string=kept,
                expected_hash=editable.raw_hash, read_dependency_guards=guards,
            ).as_dict()
        except (ValueError, edit.EditError) as error:
            # These closed error codes identify retriable writer contention.
            if _error_code(error) in {"STALE_EDIT", "STALE_SEMANTIC_WRITE", "PATH_GUARD_CHANGED"}:
                continue
            if isinstance(error, edit.EditError):
                raise ValueError(f"{error.code}: {error.reason}") from error
            raise
        return {**result, "removed": len(doomed), "outcome": "removed"}
    raise ValueError(f"STALE_EDIT: {subject} kept changing while the edge was removed")


# --------------------------------------------------------------------------- #
# The revert plan of an edge or an entity
# --------------------------------------------------------------------------- #

#: A page revert trashes through this descriptor. It is fingerprinted with the plan and never
#: sealed: the executor refuses relocation until held-fs proves namespace lineage
#: (`CURATION_RENAME_HISTORY_UNPROVABLE`), so the trash runs after the sealed steps, on the
#: caller's fingerprint, through the ordinary trash leaf.
TRASH_ENTITY_KIND = "trash-entity"


@dataclass(frozen=True)
class RevertPlan:
    promotion_id: str
    kind: str
    title: str
    #: The remove-relation steps, prepared; None when no edge needs removing.
    prepared: Any
    #: The entity pages the revert trashes.
    trash: tuple[str, ...]
    #: Inbound links from pages the revert does not change, named in the fingerprint.
    dependants: tuple[dict[str, Any], ...]
    #: Pages with removed own-write links, plus the origin; admitted by the trash preflight.
    dying: tuple[str, ...]
    #: `curation.plan_fingerprint` of the revert plan; None when nothing is left to revert.
    fingerprint: str | None
    #: The exact entity bytes whose identity and undo the caller reviewed.
    trash_hashes: tuple[tuple[str, str], ...]


def _edge_steps(
    vault_root: Path, subjects: list[dict[str, Any]], promotion_id: str,
    proofs: list[promotion_proof.Proof],
) -> list[dict[str, Any]]:
    promotion_proof.require_same(proofs)
    ledger = promotions.ledger(vault_root)

    def binding(subject: dict[str, Any]) -> dict[str, Any]:
        owner = promotion_id if subject in ledger[promotion_id]["subjects"] else next(
            key for key, row in ledger.items() if row["kind"] == "edge" and subject in row["subjects"]
        )
        # Earlier steps may change page bytes; each leaf proves the bound incarnation afresh.
        return {
            "ref": promotions.promotion_ref(owner),
            "expected_fingerprint": promotion_proof.subject_fingerprint(subject),
        }

    return [
        {
            "step_id": f"remove-{ordinal}",
            "kind": curation.REMOVE_RELATION_STEP_KIND,
            "args": {
                "subject": subject["subject"],
                "predicate": subject["predicate"],
                "target": subject["target"],
                "why": f"Revert recent promotion {promotions.promotion_ref(promotion_id)}",
                "_promotion": binding(subject),
            },
        }
        for ordinal, subject in enumerate(subjects, 1)
    ]


def _own_edges(
    vault_root: Path, entry: dict[str, Any], pages: set[str], resolve: Any
) -> list[dict[str, Any]]:
    """The edges of the same write that point at one of `pages` from a page outside them."""
    if not entry.get("write"):
        return []
    ledger = promotions.ledger(vault_root)
    found: list[dict[str, Any]] = []
    for other in ledger.values():
        if other.get("kind") != "edge" or other.get("write") != entry["write"]:
            continue
        for subject in other["subjects"]:
            if (
                subject["target"] in pages
                and subject["subject"] not in pages
                and promotions.subject_holds(vault_root, "edge", subject, resolve)
                and promotions.page_visible(vault_root, subject["subject"])
                and subject not in found
            ):
                found.append(subject)
    return found


def _inbound(vault_root: Path, path: str) -> list[dict[str, Any]]:
    from . import vault as vault_module
    from .governance import egress

    keep = egress.restricted_release_filter(vault_root)
    return [
        {"path": link.path, "line": link.line_number}
        for link in vault_module.find_inbound_wikilinks(vault_root, path, visible=keep)
    ]


def _removed_edge_lines(
    vault_root: Path, edges: list[dict[str, Any]], resolve: Any,
) -> set[tuple[str, int]]:
    """Only the relation bullet lines the revert removes, in full-page coordinates."""
    selected = {(edge["subject"], edge["predicate"], edge["target"]) for edge in edges}
    return {
        (path, line + 1)
        for path in {edge["subject"] for edge in edges}
        for line, predicate, target in promotions.authored_edges(
            vault_root, (Path(vault_root) / path).read_text(encoding="utf-8"), resolve,
        )
        if (path, predicate, target) in selected
    }


def build(vault_root: Path, ref: str) -> RevertPlan:
    """The deterministic revert plan of an edge or entity entry, from current state. Read-only."""
    from . import memory_refs
    from .vault import content_hash, read_guarded_text

    promotion_id, entry = promotions.find(vault_root, ref)
    kind = str(entry["kind"])
    if kind not in {"edge", "entity"}:
        raise ValueError(
            "INVALID_REVIEW_ACTION: a registry addition reverts through schema_memory, "
            "not curation; read the item context for the call"
        )
    proofs = promotion_proof.subjects(vault_root, kind, entry["subjects"])
    if any(proof.status == "unverified" for proof in proofs):
        raise ValueError("PROMOTION_SUBJECT_UNVERIFIED: original subject continuity is unavailable")
    resolve = promotions._Resolver(vault_root)
    holding = [subject for subject, proof in zip(entry["subjects"], proofs, strict=True) if proof.status == "same"]
    trash: list[str] = []
    dependants: list[dict[str, Any]] = []
    dying: set[str] = set()
    if kind == "edge":
        edges = holding
    else:
        trash = sorted(subject["path"] for subject in holding)
        edges = _own_edges(vault_root, entry, set(trash), resolve)
        dying = {edge["subject"] for edge in edges}
        removed_lines = _removed_edge_lines(vault_root, edges, resolve)
        origin = (entry.get("origin") or {}).get("path")
        seen: set[tuple[str, int]] = set()
        for path in trash:
            for link in _inbound(vault_root, path):
                key = (link["path"], link["line"])
                if link["path"] in {*trash, origin} or key in removed_lines or key in seen:
                    continue
                seen.add(key)
                dependants.append(link)
        dying |= {origin} if origin else set()
        dependants.sort(key=lambda link: (link["path"], link["line"]))
    title = f"Revert recent promotion {promotions.promotion_ref(promotion_id)}"
    edge_proofs = promotion_proof.subjects(vault_root, "edge", edges)
    steps = _edge_steps(vault_root, edges, promotion_id, edge_proofs)
    prepared = (
        curation.prepare_proposal(
            vault_root, {"version": 1, "title": title, "steps": steps}, allow_records=True
        )
        if steps else None
    )
    trash_steps = [
        {"step_id": f"trash-{ordinal}", "kind": TRASH_ENTITY_KIND, "args": {"path": path}}
        for ordinal, path in enumerate(trash, 1)
    ]
    trash_manifest = []
    for step, path in zip(trash_steps, trash, strict=True):
        source, _guard = read_guarded_text(vault_root, Path(vault_root) / path)
        expected_ref = next(subject["ref"] for subject in holding if subject["path"] == path)
        if memory_refs.ref_from_markdown(source) != expected_ref:
            raise ValueError("PROMOTION_SUBJECT_CHANGED: the promoted entity changed during its preview")
        trash_manifest.append({
            **step,
            "page_hash": content_hash(source),
            "dependants": [f"{link['path']}:{link['line']}" for link in dependants],
        })
    if not steps and not trash:
        fingerprint = None
    else:
        fingerprint = curation.plan_fingerprint(
            {"version": 1, "title": title, "steps": [*(prepared.plan["steps"] if prepared else []), *trash_steps]},
            [
                *(prepared.binding_manifest if prepared else []), *trash_manifest,
                # Caller review binds current evidence; leaves bind only the original incarnation.
                {"promotion_proofs": promotion_proof.fingerprint([*proofs, *edge_proofs])},
            ],
            prepared.registry_ids if prepared else {},
        )
    return RevertPlan(
        promotion_id, kind, title, prepared, tuple(trash), tuple(dependants),
        tuple(sorted(dying)), fingerprint,
        tuple((path, manifest["page_hash"]) for path, manifest in zip(trash, trash_manifest, strict=True)),
    )


def revert(
    vault_root: Path, *, review_ref: str, expected_plan_fingerprint: str, why: str
) -> dict[str, Any]:
    """Revert an edge or entity promotion in one call: recompute, check, seal, apply, trash.

    The caller's fingerprint must equal the plan recomputed from current state, so a
    page edited or linked since the caller read the item refuses and changes nothing.
    """
    plan = build(vault_root, review_ref)
    if plan.fingerprint is None:
        promotions.settle(vault_root, plan.promotion_id, "settled by state: nothing is left to revert")
        return {
            "action": "revert", "mutated": False, "ref": review_ref, "outcome": "absent",
            "removed": [], "trashed": [],
        }
    if expected_plan_fingerprint != plan.fingerprint:
        raise curation.CurationError(
            "CURATION_PLAN_IDENTITY_MISMATCH",
            "the revert fingerprint is stale; read the item context again",
        )
    expected_dead = sorted({*plan.dying, *(link["path"] for link in plan.dependants)})
    trash_hashes = dict(plan.trash_hashes)
    for path in plan.trash:
        # The trash leaf's own refusals (history, protected trees) surface before any edge is removed.
        _trash(vault_root, path, expected_dead, validate_only=True, expected_hash=trash_hashes[path])
    removed: list[dict[str, Any]] = []
    run_id = None
    if plan.prepared is not None:
        sealed = curation.seal_proposal(vault_root, plan.prepared, allow_records=True)
        run_id = sealed["run_id"]
        result = curation.apply(
            vault_root, run_id=run_id, plan_id=sealed["plan_id"],
            expected_plan_fingerprint=sealed["plan_fingerprint"], why=why,
        )
        for _ in range(len(plan.prepared.plan["steps"])):
            if result.get("phase") == "completed":
                break
            result = curation.resume(vault_root, run_id=run_id, plan_id=sealed["plan_id"])
        removed = [
            {key: step["args"][key] for key in ("subject", "predicate", "target")}
            for step in plan.prepared.plan["steps"]
        ]
    trashed = [
        _trash(vault_root, path, expected_dead, expected_hash=trash_hashes[path])
        for path in plan.trash
    ]
    promotions.settle(vault_root, plan.promotion_id, "settled by revert")
    return {
        "action": "revert", "mutated": True, "ref": review_ref, "outcome": "reverted",
        "kind": plan.kind, "run_id": run_id, "removed": removed, "trashed": trashed,
        "dependants": list(plan.dependants),
    }


def _trash(
    vault_root: Path, path: str, expected_dead: list[str], *, validate_only: bool = False,
    expected_hash: str | None = None,
) -> Any:
    from . import delete_file as delete_module

    try:
        result = delete_module.delete_file(
            vault_root, path=path, confirm=True, expected_dead_inbound=expected_dead,
            validate_only=validate_only,
            expected_hash=expected_hash,
        )
    except delete_module.DeleteFileError as error:
        raise ValueError(f"{error.code}: {error.reason}") from error
    return path if validate_only else result.as_dict()


# --------------------------------------------------------------------------- #
# The item an agent reads before it reverts
# --------------------------------------------------------------------------- #


def _item(vault_root: Path, promotion_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    from . import review_state

    kind = str(entry["kind"])
    resolve = promotions._Resolver(vault_root)
    state, _decision = review_state.ReviewStateStore(vault_root).effective_state(
        promotion_id, promotions.identity_fingerprint(promotion_id)
    )
    return {
        "category": promotions.FAMILY,
        "kind": kind,
        "state": state,
        "created": entry.get("created"),
        "new_family": bool(entry.get("new_family")),
        "subjects": [
            subject for subject in entry["subjects"]
            if promotions.subject_holds(vault_root, kind, subject, resolve)
        ],
        "omitted": int(entry.get("omitted") or 0),
    }


def item(vault_root: Path, ref: str) -> dict[str, Any]:
    """One promotion entry as the review item mode serves it: no plan is built."""
    promotion_id, entry = promotions.find(vault_root, ref)
    return {"mode": "item", "mutated": False, "ref": ref, "item": _item(vault_root, promotion_id, entry)}


def _registry_context(vault_root: Path, ref: str, entry: dict[str, Any]) -> dict[str, Any]:
    """The registry's current hash, what a `remove` would do to the keys still active, and the call.

    A registry revert is one `schema_memory` save, so its fingerprint is the hash that save
    already needs and the server decides remove or deprecate when the save runs. The preview
    reads the same decision without writing; a preview that cannot be had is said, not hidden.
    """
    from .vocabulary import contract, instances, registry_spec

    proofs = promotion_proof.subjects(vault_root, "registry", entry["subjects"])
    if any(proof.status == "unverified" for proof in proofs):
        return {"findings": [{"code": "PROMOTION_SUBJECT_UNVERIFIED", "severity": "warning"}]}
    live = [item for item, proof in zip(entry["subjects"], proofs, strict=True) if proof.status == "same"]
    if not live:
        return {}
    name, scope = str(live[0]["registry"]), live[0].get("scope")
    if any(item["registry"] != name or item.get("scope") != scope for item in entry["subjects"]):
        return {"findings": [{"code": "PROMOTION_SUBJECT_UNVERIFIED", "severity": "warning"}]}
    keys = [str(item["key"]) for item in live]
    try:
        spec = instances.select(vault_root, registry_spec(name), scope, authoring=True)
        preview = contract.propose(vault_root, spec, {"remove": keys})
    except Exception as error:  # noqa: BLE001 - an unreadable registry is reported, never guessed
        return {"findings": [{"code": _error_code(error), "severity": "error"}]}
    current = preview.get("expected_hash")
    if not isinstance(current, str):
        return {"findings": [{"code": "REGISTRY_UNAVAILABLE", "severity": "error",
                              "detail": str(preview.get("reason") or "not_readable")}]}
    arguments: dict[str, Any] = {
        "subject": name,
        "operation": "save",
        "proposal": {"remove": keys, "_promotion": {
            "ref": ref, "expected_fingerprint": promotion_proof.fingerprint(proofs),
        }},
        "expected_hash": current,
        "why": "<the reason the user gave>",
    }
    if scope:
        arguments["registry_scope"] = scope
    served: dict[str, Any] = {
        "revert_fingerprint": current,
        "dependants": [
            {"key": key, **outcome} for key, outcome in (preview.get("removal") or {}).items()
        ],
        "revert": {"tool": "schema_memory", "arguments": arguments},
    }
    if preview.get("findings"):
        served["findings"] = [dict(item) for item in preview["findings"]]
    return served


def context(vault_root: Path, ref: str, *, expected_fingerprint: str | None = None) -> dict[str, Any]:
    """The promotion, its dependants and the one call that reverts it.

    `fingerprint` is the entry's stable identity; `revert_fingerprint` is computed now,
    moves with every page the revert touches and with every dependant, and is what the
    revert call must carry.
    """
    promotion_id, entry = promotions.find(vault_root, ref)
    if expected_fingerprint and expected_fingerprint != promotions.identity_fingerprint(promotion_id):
        raise ValueError(
            f"REVIEW_ITEM_CHANGED: {ref} changed; read it again before acting"
        )
    served = _item(vault_root, promotion_id, entry)
    out: dict[str, Any] = {
        "mode": "promotion",
        "mutated": False,
        "ref": ref,
        "fingerprint": promotions.identity_fingerprint(promotion_id),
        "item": served,
        "revert_fingerprint": None,
        "dependants": [],
        "revert": None,
    }
    if entry["kind"] == "registry":
        out.update(_registry_context(vault_root, ref, entry))
    if entry["kind"] in {"edge", "entity"}:
        try:
            plan = build(vault_root, ref)
        except ValueError as error:
            if _error_code(error) != "PROMOTION_SUBJECT_UNVERIFIED":
                raise
            out["findings"] = [{"code": "PROMOTION_SUBJECT_UNVERIFIED", "severity": "warning"}]
            return out
        out["revert_fingerprint"] = plan.fingerprint
        out["dependants"] = list(plan.dependants)
        if plan.fingerprint is not None:
            out["revert"] = {
                "tool": "maintain_memory",
                "arguments": {
                    "mode": "curation",
                    "curation_action": "revert",
                    "review_ref": ref,
                    "expected_plan_fingerprint": plan.fingerprint,
                    "why": "<the reason the user gave>",
                },
            }
    return out
