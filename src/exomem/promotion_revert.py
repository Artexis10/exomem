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

from . import curation, promotions


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
    vault_root: Path, *, subject: str, predicate: str, target: str, why: str
) -> dict[str, Any]:
    """Remove every `## Relations` bullet of one (subject, predicate, target) triple.

    The leaf reads the page fresh and binds the edit to the bytes it just read, so
    it needs no hash from the caller and a concurrent edit of another part of the
    page survives: a stale hash is read again, never forced. A triple that is not
    there reports `absent`, which is also what a repeat sees.
    """
    from . import commands, edit

    if not promotions.page_visible(vault_root, subject):
        raise ValueError(f"NOT_FOUND: file does not exist: {subject}")
    for _attempt in range(_STALE_RETRIES):
        editable = edit.load_editable(vault_root, subject)
        body = editable.body
        doomed = {
            index
            for index, kind, resolved in promotions.authored_edges(vault_root, body)
            if kind == predicate and resolved == target
        }
        if not doomed:
            return {"path": subject, "removed": 0, "outcome": "absent"}
        lines = body.splitlines(keepends=True)
        region, kept = _region_without(lines, doomed, body)
        try:
            result = commands.op_edit_memory(
                vault_root,
                subject,
                why,
                operation={
                    "kind": "replace_string",
                    "old_string": region,
                    "new_string": kept,
                    "expected_hash": editable.raw_hash,
                },
            )
        except ValueError as error:
            if _error_code(error) == "STALE_EDIT":
                continue
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
    #: Pages whose links die with the revert: not dependants, and not a reason to refuse the trash.
    dying: tuple[str, ...]
    #: `curation.plan_fingerprint` of the revert plan; None when nothing is left to revert.
    fingerprint: str | None


def _edge_steps(subjects: list[dict[str, Any]], promotion_id: str) -> list[dict[str, Any]]:
    return [
        {
            "step_id": f"remove-{ordinal}",
            "kind": curation.REMOVE_RELATION_STEP_KIND,
            "args": {
                "subject": subject["subject"],
                "predicate": subject["predicate"],
                "target": subject["target"],
                "why": f"Revert recent promotion {promotions.promotion_ref(promotion_id)}",
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


def build(vault_root: Path, ref: str) -> RevertPlan:
    """The deterministic revert plan of an edge or entity entry, from current state. Read-only."""
    from .vault import content_hash

    promotion_id, entry = promotions.find(vault_root, ref)
    kind = str(entry["kind"])
    if kind not in {"edge", "entity"}:
        raise ValueError(
            "INVALID_REVIEW_ACTION: a registry addition reverts through schema_memory, "
            "not curation; read the item context for the call"
        )
    resolve = promotions._Resolver(vault_root)
    holding = [
        subject for subject in entry["subjects"]
        if promotions.subject_holds(vault_root, kind, subject, resolve)
    ]
    trash: list[str] = []
    dependants: list[dict[str, Any]] = []
    dying: set[str] = set()
    if kind == "edge":
        edges = holding
    else:
        trash = sorted(subject["path"] for subject in holding)
        edges = _own_edges(vault_root, entry, set(trash), resolve)
        dying = {edge["subject"] for edge in edges}
        origin = (entry.get("origin") or {}).get("path")
        seen: set[tuple[str, int]] = set()
        for path in trash:
            for link in _inbound(vault_root, path):
                key = (link["path"], link["line"])
                if link["path"] in {*trash, *dying, origin} or key in seen:
                    continue
                seen.add(key)
                dependants.append(link)
        dying |= {origin} if origin else set()
        dependants.sort(key=lambda link: (link["path"], link["line"]))
    title = f"Revert recent promotion {promotions.promotion_ref(promotion_id)}"
    steps = _edge_steps(edges, promotion_id)
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
    trash_manifest = [
        {
            **step,
            "page_hash": content_hash((Path(vault_root) / path).read_text(encoding="utf-8")),
            "dependants": [f"{link['path']}:{link['line']}" for link in dependants],
        }
        for step, path in zip(trash_steps, trash, strict=True)
    ]
    if not steps and not trash:
        fingerprint = None
    else:
        fingerprint = curation.plan_fingerprint(
            {"version": 1, "title": title, "steps": [*(prepared.plan["steps"] if prepared else []), *trash_steps]},
            [*(prepared.binding_manifest if prepared else []), *trash_manifest],
            prepared.registry_ids if prepared else {},
        )
    return RevertPlan(
        promotion_id, kind, title, prepared, tuple(trash), tuple(dependants),
        tuple(sorted(dying)), fingerprint,
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
    for path in plan.trash:
        # The trash leaf's own refusals (history, protected trees) surface before any edge is removed.
        _trash(vault_root, path, expected_dead, validate_only=True)
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
    trashed = [_trash(vault_root, path, expected_dead) for path in plan.trash]
    promotions.settle(vault_root, plan.promotion_id, "settled by revert")
    return {
        "action": "revert", "mutated": True, "ref": review_ref, "outcome": "reverted",
        "kind": plan.kind, "run_id": run_id, "removed": removed, "trashed": trashed,
        "dependants": list(plan.dependants),
    }


def _trash(
    vault_root: Path, path: str, expected_dead: list[str], *, validate_only: bool = False
) -> Any:
    from . import delete_file as delete_module

    try:
        result = delete_module.delete_file(
            vault_root, path=path, confirm=True, expected_dead_inbound=expected_dead,
            validate_only=validate_only,
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

    live = [item for item in entry["subjects"] if promotions.registry_key_active(vault_root, item)]
    if not live:
        return {}
    name, scope = str(live[0]["registry"]), live[0].get("scope")
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
        "proposal": {"remove": keys},
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
        plan = build(vault_root, ref)
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
