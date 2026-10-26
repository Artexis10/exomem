"""Which storage classes cell volumes use (move-cloud-cells-to-local-storage D1, D6, D7).

Hetzner Cloud Volumes stay the configured domain until the cutover (task 7.2)
switches it to the local TopoLVM class. With no local class configured, every
value here is what cellctl used before, so no render or digest moves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The Hetzner Cloud Volume class every cell used before local storage.
LEGACY_CLASS = "exomem-cloud-encrypted"

_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")


@dataclass(frozen=True)
class LocalStorage:
    """The TopoLVM class, and what its hourly backups and capacity need."""

    class_name: str = "exomem-cloud-local"
    # Immediate binding, so a clone's PV exists at once and TopoLVM pins it to
    # the source volume's node; the backup Job then follows it there (D3).
    clone_class: str = "exomem-cloud-local-clone"
    snapshot_class: str = "exomem-cloud-local-snapshot"
    driver: str = "topolvm.io"
    device_class: str = "thin"
    topology_key: str = "topology.topolvm.io/node"
    # D3: hourly backups that may run at once on one node. D6 reserves twice
    # the largest cell for each of them.
    backup_concurrency_per_node: int = 2
    # D6: the default cell size slots are counted in. 4 GiB, not the 10 GiB
    # Hetzner volumes need (their provider minimum): a local cell charges its
    # full size at ratio 1.0 and grows online.
    default_cell_gib: int = 4
    # Tasks 5.5 and 5.6: the pods cellctl reads for the host alerts, and where.
    # The chart names them (templates/_cell-storage.tpl) from the same values it
    # renders those pods with, so the two cannot disagree. Both are Prometheus
    # `/metrics` endpoints on the pod's own IP: TopoLVM's node plugin (the thin
    # pool) and node-exporter's mdadm collector (the cell array).
    metrics_namespace: str = "exomem-platform"
    thin_pool_selector: str = "app.kubernetes.io/name=topolvm,app.kubernetes.io/component=node"
    thin_pool_port: int = 8080
    array_selector: str = "app.kubernetes.io/name=exomem-cell-array-exporter"
    array_port: int = 9100

    def __post_init__(self) -> None:
        for name in (self.class_name, self.clone_class, self.snapshot_class, self.driver, self.device_class):
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ValueError("local storage names must be DNS subdomain names")
        if len({self.class_name, self.clone_class, LEGACY_CLASS}) != 3:
            raise ValueError("the local, clone and Hetzner classes must differ")
        if type(self.backup_concurrency_per_node) is not int or self.backup_concurrency_per_node < 1:
            raise ValueError("per-node backup concurrency must be a positive integer")
        if type(self.default_cell_gib) is not int or self.default_cell_gib < 1:
            raise ValueError("the default cell size must be a positive number of GiB")
        if not isinstance(self.metrics_namespace, str) or not _NAME.fullmatch(self.metrics_namespace):
            raise ValueError("the metrics namespace must be a DNS subdomain name")
        for selector in (self.thin_pool_selector, self.array_selector):
            if not isinstance(selector, str) or not selector:
                raise ValueError("a metrics pod selector must be a non-empty label selector")
        for port in (self.thin_pool_port, self.array_port):
            if type(port) is not int or not 0 < port < 65536:
                raise ValueError("a metrics port must be a TCP port number")

    @property
    def capacity_annotation(self) -> str:
        """The node annotation where TopoLVM publishes the device class's free bytes."""

        return f"capacity.{self.driver}/{self.device_class}"


@dataclass(frozen=True)
class StorageConfig:
    # D7: the one class new claims use and whose nodes publish capacity.
    domain: str = LEGACY_CLASS
    local: LocalStorage | None = None
    max_cell_gib: int = 20

    def __post_init__(self) -> None:
        minimum = self.local.default_cell_gib if self.local else LocalStorage.default_cell_gib
        if type(self.max_cell_gib) is not int or self.max_cell_gib < minimum:
            raise ValueError("max_cell_gib must be a whole number of GiB, at least the default size")
        if self.domain not in self.classes:
            raise ValueError("the cell storage domain must be a configured cell class")

    @property
    def classes(self) -> frozenset[str]:
        """D7: every class a cell volume may be bound to while cells migrate."""

        return frozenset({LEGACY_CLASS} | ({self.local.class_name} if self.local else set()))

    @property
    def domain_is_local(self) -> bool:
        return self.local is not None and self.domain == self.local.class_name

    def is_local(self, storage_class: str | None) -> bool:
        return self.local is not None and storage_class == self.local.class_name

    def claim_class(self, existing: str | None) -> str:
        """A cell keeps the class of the claim it has until it migrates (D7);
        only a new claim takes the domain. storageClassName is immutable, so
        rendering the domain over an existing claim could never apply."""

        return existing if existing in self.classes else self.domain


DEFAULT_STORAGE = StorageConfig()
