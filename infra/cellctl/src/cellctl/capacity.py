"""Observed absolute admission capacity, without a reservation ledger.

Legacy capacity is attachment based. A qualified shared envelope additionally
clamps slots by scheduler reservations and CPU/memory, preserving admission's
separate charge for every outstanding database commitment.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal

from kubernetes.utils.quantity import parse_quantity

from .manifests import JOB_CPU_REQUEST, JOB_MEMORY_REQUEST, ResourceSettings
from .storage_config import LocalStorage

GIB = 1024**3


@dataclass(frozen=True)
class CapacityConfig:
    csi_driver: str = "csi.hetzner.cloud"
    headroom: int = 0
    # Used only when a node's CSINode has no allocatable count for the
    # driver -- how the local rehearsal runs without Hetzner CSI at all.
    attachments_limit_fallback: int | None = None


@dataclass(frozen=True)
class NodeCapacity:
    cell_slots: int
    attachments_used: int
    limit_known: bool


def compute_node_capacity(
    *,
    allocatable: int | None,
    attachments_used: int,
    non_cell_attachments: int,
    config: CapacityConfig,
) -> NodeCapacity:
    limit = allocatable if allocatable is not None else config.attachments_limit_fallback
    if limit is None:
        # "Nothing is admitted to a node whose limit is unknown."
        return NodeCapacity(cell_slots=0, attachments_used=attachments_used, limit_known=False)
    cell_slots = limit - config.headroom - non_cell_attachments
    return NodeCapacity(cell_slots=cell_slots, attachments_used=attachments_used, limit_known=True)


@dataclass(frozen=True)
class LogicalVolumeRecord:
    """One TopoLVM LogicalVolume object, as capacity needs it."""

    node: str
    size: int
    device_class: str
    # lvmd has created the volume on the host (TopoLVM recorded its ID).
    created: bool
    deleting: bool


@dataclass(frozen=True)
class LocalCapacityObservation:
    """What TopoLVM publishes for the cell pool on each node (D6)."""

    # Every node, with its published free bytes, or None when its storage
    # driver publishes no pool (the control-plane server has none).
    free_bytes: dict[str, int | None] = field(default_factory=dict)
    volumes: tuple[LogicalVolumeRecord, ...] = ()
    reserved_nodes: frozenset[str] = frozenset()
    # cell id -> the node its local volume is pinned to.
    cell_nodes: dict[str, str] = field(default_factory=dict)


def compute_local_capacity(
    observation: LocalCapacityObservation, *, local: LocalStorage, cell_sizes: dict[str, int]
) -> dict[str, NodeCapacity]:
    """D6: floor((pool size - snapshot reserve) / default cell size) per node.

    TopoLVM publishes free bytes, not the pool's size, and at overprovision
    ratio 1.0 the pool is those free bytes plus the size of every volume in
    it. Snapshots and clones are volumes too, so a backup moves bytes from
    free to used and the slots stay put. The reserve is twice the largest
    cell for each backup the node may run at once. `cell_sizes` holds every
    non-deleted cell's size in GiB: admission counts each as one row, so a
    cell larger than the default takes its extra slots here.
    """

    default = local.default_cell_gib * GIB
    largest = max([local.default_cell_gib, *cell_sizes.values()]) * GIB
    reserve = 2 * largest * local.backup_concurrency_per_node
    used: Counter[str] = Counter()
    for volume in observation.volumes:
        # A volume whose object exists before lvmd creates it has taken no
        # free bytes yet, and one being deleted may already have given them
        # back. Leaving both out can only under-count.
        if volume.device_class == local.device_class and volume.created and not volume.deleting:
            used[volume.node] += volume.size
    cells_on = Counter(observation.cell_nodes.values())
    result: dict[str, NodeCapacity] = {}
    for node, free in observation.free_bytes.items():
        if free is None:
            result[node] = NodeCapacity(cell_slots=0, attachments_used=cells_on[node], limit_known=False)
            continue
        slots = 0 if node in observation.reserved_nodes else max(0, (free + used[node] - reserve) // default)
        result[node] = NodeCapacity(cell_slots=slots, attachments_used=cells_on[node], limit_known=True)

    unattributed = 0
    for cell_id, gib in sorted(cell_sizes.items()):
        extra = -(-gib // local.default_cell_gib) - 1
        node = observation.cell_nodes.get(cell_id)
        if extra > 0 and node in result and node not in observation.reserved_nodes:
            charged = min(extra, result[node].cell_slots)
            result[node] = replace(result[node], cell_slots=result[node].cell_slots - charged)
            extra -= charged
        unattributed += max(0, extra)
    # A larger cell whose volume is not on a general node yet still counts.
    for node in sorted(result, key=lambda name: (-result[name].cell_slots, name)):
        charged = min(unattributed, result[node].cell_slots)
        result[node] = replace(result[node], cell_slots=result[node].cell_slots - charged)
        unattributed -= charged
    return result


def admits_new_cell(*, non_deleted_cell_count: int, cell_slots_by_node: dict[str, int]) -> bool:
    """Substrate's own admission check: non-deleted cell rows below the sum
    of published cell_slots across every node."""

    return non_deleted_cell_count < sum(cell_slots_by_node.values())


@dataclass(frozen=True)
class SharedWorkerPolicy:
    mode: str
    profile: str
    topology_key: str
    topology_value: str
    occupancy: int
    resources: ResourceSettings
    reserve_cpu: str
    reserve_memory: str
    cell_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"selected", "all-shared"}:
            raise ValueError("shared worker mode must be selected or all-shared; omit the policy for off")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", self.profile):
            raise ValueError("shared worker profile must be a DNS label")
        if (not re.fullmatch(r"(?:[a-z0-9.-]+/)?[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", self.topology_key)
                or self.topology_key.startswith("exomem.io/")
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", self.topology_value)):
            raise ValueError("shared worker topology must be one explicit CSI label and value")
        if (not isinstance(self.cell_ids, tuple) or len(self.cell_ids) > 1024
                or any(not isinstance(cell, str) or not re.fullmatch(r"[a-z2-7]{16}", cell) for cell in self.cell_ids)
                or len(set(self.cell_ids)) != len(self.cell_ids)):
            raise ValueError("shared worker selection requires unique base32 cell IDs")
        if type(self.occupancy) is not int or self.occupancy < 1:
            raise ValueError("shared worker occupancy must be a positive qualified count")
        if not isinstance(self.resources, ResourceSettings):
            raise ValueError("shared worker resources must be ResourceSettings")
        # The selected footprint covers the existing 100m/256Mi maintenance
        # and init requests. Quota forbids concurrent demand above the serving
        # requests; observed excess is still charged during reconciliation.
        cpu = quantity(self.resources.cpu_request)
        memory = quantity(self.resources.memory_request)
        if (not Decimal("0.1") <= cpu <= 2 or not 256 * 1024**2 <= memory <= 3 * 1024**3
                or self.resources.cpu_limit != "2" or self.resources.memory_limit != "3Gi"):
            raise ValueError("shared requests must cover maintenance within the unchanged 2 CPU / 3Gi limits")
        quantity(self.reserve_cpu)
        quantity(self.reserve_memory)

    def selects(self, cell_id: str) -> bool:
        return self.mode == "all-shared" or cell_id in self.cell_ids


def quantity(value: str) -> Decimal:
    result = parse_quantity(value)
    if not result.is_finite() or result < 0:
        raise ValueError("resource quantities must be finite and non-negative")
    return result


@dataclass(frozen=True)
class NodeObservation:
    name: str
    attachment_limit: int | None = None
    cpu: Decimal = Decimal(0)
    memory: Decimal = Decimal(0)
    labels: dict[str, str] = field(default_factory=dict)
    ready: bool = False
    schedulable: bool = False
    pressure: bool = True
    taints: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class PodReservation:
    node: str | None
    cell_id: str | None
    cpu: Decimal
    memory: Decimal


@dataclass(frozen=True)
class AttachmentReservation:
    node: str
    volume: str
    cell_id: str | None


@dataclass(frozen=True)
class CapacityObservation:
    nodes: dict[str, NodeObservation] = field(default_factory=dict)
    pods: tuple[PodReservation, ...] = ()
    attachments: tuple[AttachmentReservation, ...] = ()
    incompatible_cells: frozenset[str] = frozenset()
    allocatable: dict[str, int | None] = field(default_factory=dict)
    attachments_used: dict[str, int] = field(default_factory=dict)
    non_cell_attachments: dict[str, int] = field(default_factory=dict)
    reserved_nodes: frozenset[str] = frozenset()


def effective_pod_requests(pod) -> tuple[Decimal, Decimal]:
    """Kubernetes scheduling requests, including restartable init sidecars."""
    spec = pod.spec
    status = pod.status
    statuses = {item.name: item for item in ((status.container_statuses or []) + (status.init_container_statuses or []))} if status else {}
    infeasible = bool(status and any(item.type == "PodResizePending" and item.reason == "Infeasible"
                                    for item in status.conditions or []))

    def effective(values, observed) -> tuple[Decimal, Decimal]:
        allocated = getattr(observed, "allocated_resources", None) or {}
        actual = getattr(observed, "resources", None)
        actuated = actual.requests or {} if actual else {}
        return tuple(max(quantity(values.get(name, "0")) if not infeasible or not (allocated or actuated) else Decimal(0),
                         quantity(allocated.get(name, "0")), quantity(actuated.get(name, "0")))
                     for name in ("cpu", "memory"))

    def requests(container, *, use_status: bool = True) -> tuple[Decimal, Decimal]:
        resources = container.resources
        values = resources.requests or {} if resources else {}
        observed = statuses.get(container.name) if use_status else None
        return effective(values, observed)

    regular = [Decimal(0), Decimal(0)]
    sidecars = [Decimal(0), Decimal(0)]
    peak = [Decimal(0), Decimal(0)]
    for container in spec.containers or []:
        for index, value in enumerate(requests(container)):
            regular[index] += value
    for container in spec.init_containers or []:
        restartable = getattr(container, "restart_policy", None) == "Always"
        values = requests(container, use_status=restartable)
        if restartable:
            for index, value in enumerate(values):
                sidecars[index] += value
                peak[index] = max(peak[index], sidecars[index])
        else:
            for index, value in enumerate(values):
                peak[index] = max(peak[index], sidecars[index] + value)
    result = [max(regular[index] + sidecars[index], peak[index]) for index in range(2)]
    pod_resources = getattr(spec, "resources", None)
    if pod_resources and pod_resources.requests:
        values = effective(pod_resources.requests, status)
        for index, name in enumerate(("cpu", "memory")):
            if name in pod_resources.requests:
                result[index] = values[index]
    overhead = spec.overhead or {}
    return tuple(result[index] + quantity(overhead.get(name, "0")) for index, name in enumerate(("cpu", "memory")))


def compute_shared_capacity(
    observation: CapacityObservation, *, policy: SharedWorkerPolicy,
    config: CapacityConfig, committed: frozenset[str],
    storage_slots: dict[str, NodeCapacity] | None = None,
) -> dict[str, NodeCapacity]:
    """`storage_slots` is the local pool's storage term (D6), given once local
    storage is the domain. It then replaces the attachment term, and the
    footprint carries the hourly backup Job that runs beside a serving cell."""
    present = set(observation.nodes) | set(observation.allocatable) | set(observation.attachments_used)
    result = {name: NodeCapacity(0, observation.attachments_used.get(name, 0), True) for name in present}
    workers = [node for node in observation.nodes.values()
               if node.labels.get("exomem.io/shared-profile") == policy.profile]
    # A scalar ceiling cannot force balanced packing on a second worker.
    if len(workers) != 1 or observation.incompatible_cells & committed:
        return result
    node = workers[0]
    allowed_taint = ("exomem.io/shared-profile", policy.profile, "NoSchedule")
    if (not node.ready or not node.schedulable or node.pressure or allowed_taint not in node.taints
            or "exomem.io/dedicated-cell" in node.labels
            or node.labels.get(policy.topology_key) != policy.topology_value
            or any(taint[0] == "exomem.io/dedicated-cell" or
                   (taint[2] in {"NoSchedule", "NoExecute"} and taint != allowed_taint) for taint in node.taints)):
        return result
    storage = storage_slots.get(node.name) if storage_slots is not None else None
    if storage_slots is not None and (storage is None or not storage.limit_known):
        result[node.name] = NodeCapacity(0, storage.attachments_used if storage else 0, False)
        return result
    if storage_slots is None and node.attachment_limit is None:
        result[node.name] = NodeCapacity(0, observation.attachments_used.get(node.name, 0), False)
        return result
    footprint = (quantity(policy.resources.cpu_request), quantity(policy.resources.memory_request))
    if storage is not None:
        footprint = (footprint[0] + quantity(JOB_CPU_REQUEST), footprint[1] + quantity(JOB_MEMORY_REQUEST))
    other = [Decimal(0), Decimal(0)]
    cells: dict[str, list[Decimal]] = {}
    for pod in observation.pods:
        if pod.node not in {None, node.name}:
            continue
        target = cells.setdefault(pod.cell_id, [Decimal(0), Decimal(0)]) if pod.cell_id in committed else other
        target[0] += pod.cpu
        target[1] += pod.memory
    for reservation in cells.values():
        for index in range(2):
            other[index] += max(Decimal(0), reservation[index] - footprint[index])
    # One ordinary committed cell volume is already charged by admission.
    # Orphans, unknown ownership and additional volumes are not.
    volumes: dict[str, set[str]] = {}
    extra_attachments = 0
    seen_volumes: set[str] = set()
    for attachment in observation.attachments:
        if attachment.node != node.name or attachment.volume in seen_volumes:
            continue
        seen_volumes.add(attachment.volume)
        if attachment.cell_id in committed:
            volumes.setdefault(attachment.cell_id, set()).add(attachment.volume)
        else:
            extra_attachments += 1
    extra_attachments += sum(max(0, len(names) - 1) for names in volumes.values())
    cpu_slots = int((node.cpu - other[0] - quantity(policy.reserve_cpu)) // footprint[0])
    memory_slots = int((node.memory - other[1] - quantity(policy.reserve_memory)) // footprint[1])
    if storage is not None:
        storage_term, used = storage.cell_slots, storage.attachments_used
    else:
        storage_term = node.attachment_limit - config.headroom - extra_attachments
        used = observation.attachments_used.get(node.name, 0)
    slots = max(0, min(policy.occupancy, cpu_slots, memory_slots, storage_term))
    result[node.name] = NodeCapacity(slots, used, True)
    return result
