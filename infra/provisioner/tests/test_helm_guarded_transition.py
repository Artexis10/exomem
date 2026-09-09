from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_provider_adapters import _metadata

from exomem_provisioner.adapters import HelmCliAdapter
from exomem_provisioner.conflict_reason import ConflictReason
from exomem_provisioner.driver import DriverRetryable
from exomem_provisioner.lifecycle import MetadataConflict
from exomem_provisioner.repository import ClaimConflict

CHART = "exomem-cell-0.1.0"
TARGET = {"workloadMode": "serve", "image": "target"}


class ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__("private-provider-sentinel")
        self.status = status


class HelmReleaseConfigMaps:
    """Only the two Kubernetes calls the pending-record cleanup is allowed to make."""

    def __init__(self, history: list[dict]):
        self.history = history
        self.reads: list[tuple[str, str]] = []
        self.deletes: list[tuple[str, str, dict]] = []
        self.records: dict[str, SimpleNamespace] = {}
        self.delete_error: Exception | None = None

    def record(self, name: str, *, namespace: str, labels: dict[str, str]) -> None:
        self.records[name] = SimpleNamespace(
            metadata=SimpleNamespace(
                name=name,
                namespace=namespace,
                uid="uid-" + name,
                resource_version="rv-" + name,
                labels=labels,
            )
        )

    def read_namespaced_config_map(self, name, namespace):
        self.reads.append((name, namespace))
        if name not in self.records:
            raise ApiError(404)
        return self.records[name]

    def delete_namespaced_config_map(self, name, namespace, body=None):
        self.deletes.append((name, namespace, body))
        if self.delete_error is not None:
            raise self.delete_error
        self.records.pop(name, None)
        revision = int(name.rsplit(".v", 1)[1])
        self.history[:] = [item for item in self.history if item["revision"] != revision]


class HelmTransport:
    def __init__(self, tmp_path: Path):
        self.calls = []
        self.lost = False
        self.fail = None
        self.current = {"workloadMode": "serve", "image": "old"}
        self.history = [{"revision": 1, "status": "deployed", "chart": CHART}]
        self.revision_values: dict[int, dict] = {}
        self.core = HelmReleaseConfigMaps(self.history)
        self.adapter = HelmCliAdapter(
            binary="helm",
            expected_version="3.19.4",
            chart_path="chart",
            chart_version="0.1.0",
            runner=self.run,
            temporary_directory=tmp_path,
            core_v1=self.core,
        )

    def leave_pending(self, status, *, revision, values=None, chart=CHART, record=True):
        """Arm the record a provisioner killed mid `helm upgrade --wait` leaves behind."""
        namespace = _metadata().resource_name
        if status == "pending-install":
            self.history[:] = []
        self.history.append({"revision": revision, "status": status, "chart": chart})
        self.revision_values[revision] = TARGET if values is None else values
        name = f"sh.helm.release.v1.{namespace}.v{revision}"
        if record:
            self.core.record(
                name,
                namespace=namespace,
                labels={
                    "owner": "helm",
                    "name": namespace,
                    "status": status,
                    "version": str(revision),
                },
            )
        return name

    async def run(self, argv, environment):
        assert environment == {"HELM_DRIVER": "configmap"}
        self.calls.append(argv)
        if argv[1] == "version":
            stdout = "v3.19.4\n"
        elif argv[1:3] == ("get", "values"):
            if "--revision" in argv:
                stdout = json.dumps(self.revision_values[int(argv[argv.index("--revision") + 1])])
            else:
                stdout = json.dumps(self.current)
        elif argv[1] == "history":
            if not self.history:
                return SimpleNamespace(returncode=1, stdout="", stderr="private-provider-sentinel")
            stdout = json.dumps(self.history)
        else:
            assert argv[1] == "upgrade"
            values_path = Path(argv[argv.index("--values") + 1])
            assert values_path.stat().st_mode & 0o777 == 0o600
            assert json.loads(values_path.read_text())["image"] == "target"
            self.current = json.loads(values_path.read_text())
            if self.fail == "os-error":
                raise OSError("private-provider-sentinel")
            if self.fail == "timeout":
                raise TimeoutError("private-provider-sentinel")
            return SimpleNamespace(
                returncode=1 if self.fail else 0, stdout="", stderr="private-provider-sentinel"
            )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    async def guard(self):
        self.calls.append(("guard",))
        if self.lost:
            raise ClaimConflict("claim lost")

    async def transition(self, api, **kwargs):
        values = {"workloadMode": "serve", "image": "target"}
        if api == "ensure":
            return await self.adapter.ensure_release(_metadata(), values, **kwargs)
        return await self.adapter.transition_release(
            _metadata(),
            values,
            operation_id="migration-operation",
            **kwargs,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["ensure", "transition"])
async def test_guard_runs_after_predecessor_reads_and_immediately_before_helm_upgrade(
    tmp_path, api
):
    h = HelmTransport(tmp_path)
    await h.transition(api, rollback_on_failure=False, effect_guard=h.guard)
    assert h.calls[-2] == ("guard",)
    assert h.calls[-1][1] == "upgrade"
    assert "--atomic" not in h.calls[-1]
    assert "--wait" in h.calls[-1] and "--wait-for-jobs" in h.calls[-1]
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["ensure", "transition"])
async def test_claim_loss_prevents_helm_effect_and_cleans_private_values(tmp_path, api):
    h = HelmTransport(tmp_path)
    h.lost = True
    with pytest.raises(ClaimConflict, match="claim lost"):
        await h.transition(api, rollback_on_failure=False, effect_guard=h.guard)
    assert h.calls[-1] == ("guard",)
    assert not any(len(call) > 1 and call[1] == "upgrade" for call in h.calls)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["ensure", "transition"])
@pytest.mark.parametrize("failure", ["exit-status", "os-error", "timeout"])
async def test_irreversible_helm_failure_is_retryable_without_automatic_rollback(
    tmp_path, api, failure
):
    h = HelmTransport(tmp_path)
    h.fail = failure
    with pytest.raises(DriverRetryable) as raised:
        await h.transition(api, rollback_on_failure=False, effect_guard=h.guard)
    assert "private-provider-sentinel" not in str(raised.value)
    assert "--atomic" not in h.calls[-1]
    assert all(call[1:2] != ("rollback",) for call in h.calls)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["ensure", "transition"])
async def test_existing_helm_calls_keep_atomic_rollback_by_default(tmp_path, api):
    h = HelmTransport(tmp_path)
    await h.transition(api)
    assert "--atomic" in h.calls[-1]
    assert ("guard",) not in h.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["ensure", "transition"])
@pytest.mark.parametrize(
    "options",
    [
        {"rollback_on_failure": "false"},
        {"rollback_on_failure": 0},
        {"effect_guard": "not-callable"},
    ],
)
async def test_malformed_helm_effect_options_refuse_before_a_provider_call(tmp_path, api, options):
    h = HelmTransport(tmp_path)
    with pytest.raises(MetadataConflict):
        await h.transition(api, **options)
    assert not h.calls
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit-status", "os-error", "timeout"])
async def test_replay_reconciles_an_uncertain_target_even_if_helm_saved_its_values(
    tmp_path, failure
):
    h = HelmTransport(tmp_path)
    h.fail = failure
    with pytest.raises(DriverRetryable):
        await h.transition("transition", rollback_on_failure=False, effect_guard=h.guard)
    h.fail = None
    first_target = dict(h.current)
    await h.transition("transition", rollback_on_failure=False, effect_guard=h.guard)
    assert sum(call[1:2] == ("upgrade",) for call in h.calls) == 2
    assert h.current == first_target
    assert h.calls[-2] == ("guard",)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_guard_error_is_not_reclassified_as_an_uncertain_helm_effect(tmp_path):
    h = HelmTransport(tmp_path)

    async def guard():
        raise OSError("guard unavailable")

    with pytest.raises(OSError, match="guard unavailable"):
        await h.transition("ensure", rollback_on_failure=False, effect_guard=guard)
    assert not any(call[1:2] == ("upgrade",) for call in h.calls)
    assert not list(tmp_path.iterdir())


def _upgrades(transport: HelmTransport) -> int:
    return sum(call[1:2] == ("upgrade",) for call in transport.calls)


@pytest.mark.asyncio
async def test_own_pending_upgrade_record_is_cleared_once_then_the_exact_target_retried(
    tmp_path,
):
    h = HelmTransport(tmp_path)
    name = h.leave_pending("pending-upgrade", revision=2)
    # The same values, written back in a different key order: equality is canonical.
    h.revision_values[2] = dict(reversed(list(TARGET.items())))

    await h.transition("ensure", rollback_on_failure=False, effect_guard=h.guard)

    assert h.core.deletes == [
        (
            name,
            _metadata().resource_name,
            {
                "propagationPolicy": "Foreground",
                "preconditions": {"uid": "uid-" + name, "resourceVersion": "rv-" + name},
            },
        )
    ]
    assert h.calls[h.calls.index(("guard",)) + 1][1] != "upgrade"
    assert h.calls[-2] == ("guard",) and h.calls[-1][1] == "upgrade"
    assert _upgrades(h) == 1
    assert h.history == [{"revision": 1, "status": "deployed", "chart": CHART}]
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_own_pending_install_record_is_cleared_with_no_deployed_predecessor(tmp_path):
    h = HelmTransport(tmp_path)
    name = h.leave_pending("pending-install", revision=1)

    await h.transition("ensure", rollback_on_failure=False, effect_guard=h.guard)

    assert [call[0] for call in h.core.deletes] == [name]
    assert h.history == []
    assert _upgrades(h) == 1
    assert "--install" in h.calls[-1]
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pending",
    [
        {"values": {"workloadMode": "serve", "image": "someone-elses"}},
        {"chart": "exomem-cell-0.2.0"},
    ],
)
async def test_foreign_pending_release_is_terminal_without_a_delete_or_an_upgrade(
    tmp_path, pending
):
    h = HelmTransport(tmp_path)
    h.leave_pending("pending-upgrade", revision=2, **pending)

    with pytest.raises(MetadataConflict) as raised:
        await h.transition("ensure", rollback_on_failure=False, effect_guard=h.guard)

    assert raised.value.reason is ConflictReason.HELM_PENDING_RELEASE_IS_FOREIGN
    assert h.core.deletes == []
    assert _upgrades(h) == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_a_pending_record_behind_the_latest_revision_is_never_cleared(tmp_path):
    h = HelmTransport(tmp_path)
    h.history[:] = [
        {"revision": 1, "status": "pending-upgrade", "chart": CHART},
        {"revision": 2, "status": "deployed", "chart": CHART},
    ]
    h.revision_values[1] = dict(TARGET)

    with pytest.raises(MetadataConflict) as raised:
        await h.transition("ensure", rollback_on_failure=False, effect_guard=h.guard)

    assert raised.value.reason is ConflictReason.HELM_PENDING_RELEASE_IS_FOREIGN
    assert h.core.deletes == []
    assert _upgrades(h) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [ApiError(409), ApiError(503), OSError("private-provider-sentinel")]
)
async def test_a_lost_delete_acknowledgement_is_retryable_and_never_deleted_twice(tmp_path, error):
    h = HelmTransport(tmp_path)
    h.leave_pending("pending-upgrade", revision=2)
    h.core.delete_error = error

    with pytest.raises(DriverRetryable) as raised:
        await h.transition("ensure", rollback_on_failure=False, effect_guard=h.guard)

    assert "private-provider-sentinel" not in str(raised.value)
    assert len(h.core.deletes) == 1
    assert _upgrades(h) == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_an_unproven_pending_removal_is_retryable_and_never_upgrades(tmp_path):
    h = HelmTransport(tmp_path)
    h.leave_pending("pending-upgrade", revision=2)
    h.core.records.clear()  # the API acknowledges nothing; the record still stands

    with pytest.raises(DriverRetryable):
        await h.transition("ensure", rollback_on_failure=False, effect_guard=h.guard)

    assert h.core.deletes == []
    assert _upgrades(h) == 0


@pytest.mark.asyncio
async def test_a_lost_claim_prevents_the_pending_record_delete(tmp_path):
    h = HelmTransport(tmp_path)
    h.leave_pending("pending-upgrade", revision=2)
    h.lost = True

    with pytest.raises(ClaimConflict, match="claim lost"):
        await h.transition("ensure", rollback_on_failure=False, effect_guard=h.guard)

    assert h.core.deletes == []
    assert _upgrades(h) == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_an_ungoverned_call_never_touches_a_pending_record(tmp_path):
    h = HelmTransport(tmp_path)
    h.leave_pending("pending-upgrade", revision=2)

    await h.transition("ensure")

    assert h.core.reads == [] and h.core.deletes == []
    assert _upgrades(h) == 1
