"""Read-only receipt verification for internal episode coordination.

The state must come from the episode owner, not a public caller.  This adapter
verifies curation evidence; it neither authorizes disclosure nor persists state,
executes a leaf, repairs a receipt, or proves a missing write did not commit.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import curation, episode_model


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
) -> dict[str, Any]:
    """Return a reconciled copy only for an evidenced, currently verified commit.

    All receipt, approval, plan and witness bytes come from the existing canonical
    curation store.  A receipt gap stays uncertain until that store's executor
    recovers it.  Historical commits whose postimages changed also stay uncertain:
    the current episode model cannot express historical commitment separately from
    current effect coverage.  Neither case becomes permission to retry.

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
        )
        if witness is None or receipts[0]["result_digest"] != witness["result_digest"]:
            raise _uncertain("receipt and atomic witness do not prove the same result")
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
