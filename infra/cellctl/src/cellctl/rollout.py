"""D6 image selection: which image a cell runs, and which cells upgrade next.

Pure functions, no I/O. `select_upgrade_candidates` is the one cross-cell
decision cellctl makes before calling `decide.decide()` once per row; every
other function here is single-row.
"""

from __future__ import annotations

from datetime import datetime

from .state import CellRow, ClusterObservation, RolloutRow


def current_image(row: CellRow, observation: ClusterObservation) -> str | None:
    """The image on the cell's StatefulSet, or observed_image once it is gone."""

    if observation.statefulset_image:
        return observation.statefulset_image
    return row.observed_image


def target_image(row: CellRow, cell_image: str | None) -> str | None:
    """desired_image if set, otherwise the settings row's cell_image."""

    return row.desired_image or cell_image


def initial_image(row: CellRow, rollout: RolloutRow, cell_image: str | None) -> tuple[str | None, str | None]:
    """Which image a cell with no StatefulSet yet should start on.

    Returns (image, error_code); error_code is NO_GOOD_IMAGE when nothing is
    usable (paused rollout, no last_good_image).
    """

    from .state import NO_GOOD_IMAGE

    if row.desired_image:
        return row.desired_image, None
    if not rollout.paused and cell_image:
        return cell_image, None
    if rollout.last_good_image:
        return rollout.last_good_image, None
    return None, NO_GOOD_IMAGE


def is_candidate(row: CellRow, observation: ClusterObservation, cell_image: str | None) -> bool:
    """A serving cell whose image differs from its target."""

    return _is_candidate(row, observation, cell_image)


def _is_candidate(row: CellRow, observation: ClusterObservation, cell_image: str | None) -> bool:
    if row.desired_state not in ("running", "read_only"):
        return False
    current = current_image(row, observation)
    target = target_image(row, cell_image)
    if current is None or target is None:
        return False
    return current != target


def should_attempt_upgrade(
    row: CellRow,
    rollout: RolloutRow,
    observation: ClusterObservation,
    cell_image: str | None,
    *,
    now: datetime,
    refused: bool = False,
) -> bool:
    if rollout.paused:
        return False
    if observation.statefulset_hold_kind or row.hold_kind:
        return False
    # D4/D6: readiness is read from this pass's observation of the pod on
    # the update revision, not only from the row, which an earlier pass wrote.
    if not (row.ready and observation.pod_ready):
        return False
    # D4: a row whose last apply was refused for its current generation,
    # render digest and image starts no attempt.
    if refused:
        return False
    # D6: not inside a backup backoff.
    retry_after = observation.statefulset_backup_retry_after
    if retry_after is not None and retry_after > now:
        return False
    return _is_candidate(row, observation, cell_image)


def select_upgrade_candidates(
    rows: list[CellRow],
    observations: dict[str, ClusterObservation],
    rollout: RolloutRow,
    cell_image: str | None,
    *,
    now: datetime,
    restore_active: bool,
    upgrading_per_node: dict[str | None, int] | None = None,
    concurrency_per_node: int = 1,
    refused: frozenset[str] = frozenset(),
    due: frozenset[str] | None = None,
) -> set[str]:
    """automate-cloud-releases D4: the cells that start an attempt this pass.
    Only the canary may start while its image differs from its target. An
    ineligible canary blocks the fleet. After the canary, eligible cells fill
    each PV node's quota in rollout-priority order, or oldest candidate first
    when windows supply a due set. A restore hold blocks every start."""

    if restore_active or rollout.paused or not rows:
        return set()

    owner = owner_cell(rows)
    owner_target = target_image(owner, cell_image)
    if owner_target is not None and current_image(owner, observations[owner.cell_id]) != owner_target:
        # The canary's own attempt occupies its node's quota too.
        if (upgrading_per_node or {}).get(observations[owner.cell_id].pv_node, 0) >= concurrency_per_node:
            return set()
        if should_attempt_upgrade(
            owner, rollout, observations[owner.cell_id], cell_image, now=now, refused=owner.cell_id in refused
        ):
            return {owner.cell_id}
        return set()

    eligible = [
        row
        for row in rows
        if _is_candidate(row, observations[row.cell_id], cell_image)
        and should_attempt_upgrade(
            row, rollout, observations[row.cell_id], cell_image, now=now, refused=row.cell_id in refused
        )
        and (due is None or row.cell_id in due)
    ]
    if due is None:
        eligible.sort(key=lambda row: (row.rollout_priority, row.cell_id))
    else:
        eligible.sort(key=lambda row: (observations[row.cell_id].statefulset_candidate_since or now,
                                       row.cell_id))
    counts = dict(upgrading_per_node or {})
    chosen: set[str] = set()
    for row in eligible:
        node = observations[row.cell_id].pv_node
        if counts.get(node, 0) < concurrency_per_node:
            counts[node] = counts.get(node, 0) + 1
            chosen.add(row.cell_id)
    return chosen


def owner_cell(rows: list[CellRow]) -> CellRow:
    """D6: the owner's cell -- the non-deleted row with the lowest
    `rollout_priority`. It goes first in a rollout and in a relocation."""

    return min(rows, key=lambda row: (row.rollout_priority, row.cell_id))


def parked_canary(
    rows: list[CellRow],
    observations: dict[str, ClusterObservation],
    cell_image: str | None,
    refused: frozenset[str],
) -> str | None:
    """D6: the owner cell, when the rollout is waiting on it because its
    last apply was refused, so the wait is visible on the rollout row."""

    if not rows:
        return None
    owner = owner_cell(rows)
    owner_target = target_image(owner, cell_image)
    if owner.cell_id not in refused or owner_target is None:
        return None
    if current_image(owner, observations[owner.cell_id]) == owner_target:
        return None
    return owner.cell_id
