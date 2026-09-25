"""D6 image selection: which image a cell runs, and which cell upgrades next.

Pure functions, no I/O. `select_upgrade_candidate` is the one cross-cell
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


def select_upgrade_candidate(
    rows: list[CellRow],
    observations: dict[str, ClusterObservation],
    rollout: RolloutRow,
    cell_image: str | None,
    *,
    now: datetime,
    any_cell_already_upgrading: bool,
    refused: frozenset[str] = frozenset(),
) -> str | None:
    """The next cell eligible to start an upgrade attempt this pass, or None.

    D6: canary first. The canary is the owner's cell -- the non-deleted row
    with the lowest `rollout_priority`. No other cell starts an attempt while
    the canary's image differs from its target: a canary that is stopped,
    not Ready, not yet provisioned or in backoff is waited for, never
    skipped. Once the canary runs the target, every other candidate is
    picked in `rollout_priority` then `cell_id` order, and an ineligible one
    is skipped rather than waited for -- ordering among tenants protects
    nothing, and waiting on one broken tenant would hold the fleet back.
    """

    if any_cell_already_upgrading or rollout.paused or not rows:
        return None

    owner = min(rows, key=lambda row: (row.rollout_priority, row.cell_id))
    owner_target = target_image(owner, cell_image)
    if owner_target is not None and current_image(owner, observations[owner.cell_id]) != owner_target:
        if should_attempt_upgrade(
            owner, rollout, observations[owner.cell_id], cell_image, now=now, refused=owner.cell_id in refused
        ):
            return owner.cell_id
        return None

    eligible = [
        row
        for row in rows
        if _is_candidate(row, observations[row.cell_id], cell_image)
        and should_attempt_upgrade(
            row, rollout, observations[row.cell_id], cell_image, now=now, refused=row.cell_id in refused
        )
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda row: (row.rollout_priority, row.cell_id)).cell_id


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
    owner = min(rows, key=lambda row: (row.rollout_priority, row.cell_id))
    owner_target = target_image(owner, cell_image)
    if owner.cell_id not in refused or owner_target is None:
        return None
    if current_image(owner, observations[owner.cell_id]) == owner_target:
        return None
    return owner.cell_id
