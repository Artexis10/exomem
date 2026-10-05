"""Read-only receipt verification for internal episode coordination.

The state must come from the episode owner, not a public caller.  This adapter
verifies curation evidence; it neither authorizes disclosure nor persists state,
executes a leaf, repairs a receipt, or proves a missing write did not commit.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import curation, episode_model
from .governance import egress
from .governance.principal import effective_principal

#: `current_coverage`'s default: the release filter of the current caller.
_CALLER = object()


def _release_filter(vault_root: Path) -> Callable[[str], bool] | None:
    """The current caller's write-door release filter; None sees everything."""
    return egress.restricted_release_filter(vault_root, principal=effective_principal())


def _uncertain(reason: str) -> episode_model.EpisodeError:
    return episode_model.EpisodeError("EPISODE_OUTCOME_UNCERTAIN", reason)


def reconcile_curation_leaf(
    vault_root: Path,
    state: Mapping[str, Any],
    candidate_id: str,
    leaf_id: str,
) -> dict[str, Any]:
    """Verify an internal uncertain leaf under the existing vault read boundary.

    The consistency guard excludes canonical writers throughout the evidence
    read, including multi-path postimages, without acquiring writer authority.
    It is reentrant when the future episode owner already holds that boundary.
    """
    from .writer_lease import active_manager

    with active_manager().consistency_guard(
        vault_root, operation="episode-reconciliation", holder_kind="command"
    ):
        return _reconcile_held(vault_root, state, candidate_id, leaf_id)


def _reconcile_held(
    vault_root: Path,
    state: Mapping[str, Any],
    candidate_id: str,
    leaf_id: str,
    *,
    live: bool = True,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a reconciled copy only for an evidenced, currently verified commit.

    `live=False` verifies the historical commitment alone -- plan, approval,
    receipt and atomic witness -- and leaves the live postimage to the caller;
    `evidence`, when given, receives the verified witness, step and binding.

    All receipt, approval, plan and witness bytes come from the existing canonical
    curation store.  A receipt gap stays uncertain until that store's executor
    recovers it. With `live=True`, a changed postimage also stays uncertain;
    historical coverage uses `live=False` and verifies the chain tip separately.
    Neither case becomes permission to retry.

    A failed receipt cannot prove noncommit for this episode attempt: the current
    binding does not identify a curation attempt, and an older failure may predate
    a newer in-flight attempt.  Noncommit reconciliation needs that later contract.
    """
    snapshot = episode_model._copy(state)
    leaf = episode_model._owned(snapshot, candidate_id, leaf_id)
    binding = leaf["binding"]
    if leaf["outcome"] != "uncertain" or binding is None:
        raise episode_model.EpisodeError(
            "EPISODE_RECONCILIATION_INVALID", "only a bound uncertain leaf may reconcile"
        )

    store = curation.CurationStore(Path(vault_root))
    try:
        run = binding["run_id"]
        plan = store.load_plan(run)
        identity, fingerprint = store.identities(run)
        # Revalidate the original effect and every binding component against
        # the stored seal, rather than trusting identifiers in the state alone.
        verified_binding = episode_model._binding({**binding, "sealed_plan": plan}, leaf)
        if (identity, fingerprint) != (
            verified_binding["plan_id"],
            verified_binding["plan_fingerprint"],
        ):
            raise _uncertain("stored plan identity differs from the episode binding")
        store.validated_approval(run, required=True)
        reconstructed = store.reconstruct(run)
        if reconstructed["phase"] == "blocked":
            raise _uncertain("curation evidence is blocked")

        operation = verified_binding["operation_id"]
        receipts = [
            receipt
            for receipt in reconstructed["receipts"]
            if receipt["operation_id"] == operation
            and receipt["outcome"] in {"committed", "recovered-committed"}
        ]
        if len(receipts) != 1:
            raise _uncertain("one verified committed receipt is required")
        ordinal = verified_binding["ordinal"]
        witness = curation._validated_operation_witness(
            store,
            run,
            plan_identity=identity,
            ordinal=ordinal,
            step=plan["steps"][ordinal],
            binding=plan["binding_manifest"][ordinal],
            operation_identity=operation,
            compensation=False,
            live=live,
        )
        if witness is None or receipts[0]["result_digest"] != witness["result_digest"]:
            raise _uncertain("receipt and atomic witness do not prove the same result")
        if evidence is not None:
            evidence.update(
                witness=witness,
                step=plan["steps"][ordinal],
                binding=plan["binding_manifest"][ordinal],
            )
    except curation.CurationError as error:
        raise _uncertain(
            "curation evidence is unavailable, invalid or no longer current"
        ) from error

    observation = episode_model.VerifiedLeafOutcome(
        candidate_id=candidate_id,
        leaf_id=leaf_id,
        operation_id=operation,
        effect_digest=leaf["effect_digest"],
        outcome="committed",
        receipt_digest=curation._digest(receipts[0]),
        result_digest=witness["result_digest"],
        readback_digest=curation._digest(witness["after"]),
    )
    return episode_model.reconcile_leaf(snapshot, observation)


def _mark(item: Mapping[str, Any]) -> str | None:
    """One postimage or preimage entry as a comparable state of its path."""
    if item.get("absent") is True:
        return "absent"
    value = item.get("content_hash")
    return value if isinstance(value, str) else None


def _tip_is_live(
    vault_root: Path,
    path: str,
    after: str,
    evidence: Mapping[str, Any],
    keep: Callable[[str], bool] | None,
) -> bool:
    """Whether the live page at `path` is the last leaf's recorded result.

    A page this caller may not read is not live for it, exactly as a page
    that is gone: the attestation must not tell withheld from deleted.
    """
    from . import curation

    if after != "absent" and keep is not None and not keep(path):
        return False
    try:
        if evidence["step"]["kind"] in curation.RECORDS_STEP_KINDS:
            # A Records item is current only while its Records receipt agrees.
            curation._verify_records_receipt(  # noqa: SLF001
                vault_root, evidence["binding"], evidence["witness"], kind=evidence["step"]["kind"]
            )
        if after == "absent":
            return curation._guarded_absent(vault_root, path)  # noqa: SLF001
        return curation._read_target(vault_root, path)[1] == after  # noqa: SLF001
    except curation.CurationError:
        return False


def current_coverage(
    vault_root: Path, state: Mapping[str, Any], *, keep: Any = _CALLER
) -> dict[str, bool]:
    """Whether each committed leaf's commitment still holds as current coverage.

    `keep` is the caller's release filter (default: the current caller's); a
    last leaf whose page it withholds is unproven, as for a deleted page.

    Historical commitment is verified per leaf from its own evidence -- plan,
    approval, receipt and atomic witness -- and must reproduce the proof the
    ledger recorded. Current coverage is then a chain per path: each earlier
    leaf's recorded result must equal the next leaf's recorded starting state,
    and only the last leaf on the path is checked against the live page. Any
    gap -- a missing receipt, a mismatched hash, a fork, a cycle, or a live page
    that differs from the last leaf -- marks every leaf on that path unproven.
    Read-only, under the existing consistency boundary.
    """
    from .writer_lease import active_manager

    committed = [
        (candidate["candidate_id"], leaf)
        for candidate in state["candidates"]
        for leaf in candidate["leaves"]
        if leaf["outcome"] == "committed"
    ]
    verdict = {leaf["leaf_id"]: False for _candidate, leaf in committed}
    if keep is _CALLER:
        keep = _release_filter(vault_root)
    with active_manager().consistency_guard(
        vault_root, operation="episode-coverage-chain", holder_kind="command"
    ):
        chains: dict[str, list[tuple[str, str | None, str | None, dict[str, Any]]]] = {}
        for candidate_id, leaf in committed:
            trial = episode_model._copy(state)
            episode_model._owned(trial, candidate_id, leaf["leaf_id"]).update(
                outcome="uncertain", outcome_proof=None
            )
            evidence: dict[str, Any] = {}
            try:
                verified = _reconcile_held(
                    vault_root, trial, candidate_id, leaf["leaf_id"], live=False, evidence=evidence
                )
            except episode_model.EpisodeError as error:
                if error.code != "EPISODE_OUTCOME_UNCERTAIN":
                    raise
                continue
            proof = episode_model._owned(verified, candidate_id, leaf["leaf_id"])["outcome_proof"]
            if proof != leaf["outcome_proof"]:
                continue
            verdict[leaf["leaf_id"]] = True
            before = {
                str(item.get("path")): _mark(item)
                for item in evidence["witness"]["before"]
                if isinstance(item, Mapping)
            }
            for item in evidence["witness"]["after"]:
                path = str(item["path"])
                chains.setdefault(path, []).append(
                    (leaf["leaf_id"], before.get(path), _mark(item), evidence)
                )
        broken: set[str] = set()
        for path, nodes in chains.items():
            successors = {
                node[0]: [
                    other[0]
                    for other in nodes
                    if other[0] != node[0] and other[1] is not None and other[1] == node[2]
                ]
                for node in nodes
            }
            predecessors = {
                node[0]: [leaf for leaf, after in successors.items() if node[0] in after]
                for node in nodes
            }
            tips = [leaf for leaf, after in successors.items() if not after]
            starts = [leaf for leaf, before in predecessors.items() if not before]
            ordered: list[str] = []
            if (
                len(tips) == 1
                and len(starts) == 1
                and all(len(after) <= 1 for after in successors.values())
                and all(len(before) <= 1 for before in predecessors.values())
            ):
                cursor: str | None = starts[0]
                while cursor is not None and cursor not in ordered:
                    ordered.append(cursor)
                    cursor = (successors[cursor] or [None])[0]
            tip = next((node for node in nodes if node[0] == ordered[-1]), None) if ordered else None
            if (
                len(ordered) != len(nodes)
                or tip is None
                or tip[2] is None
                or not _tip_is_live(vault_root, path, tip[2], tip[3], keep)
            ):
                broken.update(node[0] for node in nodes)
        for leaf_id in broken:
            verdict[leaf_id] = False
    return verdict
