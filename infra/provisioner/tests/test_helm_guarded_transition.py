from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_provider_adapters import _metadata

from exomem_provisioner.adapters import HelmCliAdapter
from exomem_provisioner.driver import DriverRetryable
from exomem_provisioner.lifecycle import MetadataConflict
from exomem_provisioner.repository import ClaimConflict


class HelmTransport:
    def __init__(self, tmp_path: Path):
        self.calls = []
        self.lost = False
        self.fail = None
        self.current = {"workloadMode": "serve", "image": "old"}
        self.adapter = HelmCliAdapter(
            binary="helm",
            expected_version="3.19.4",
            chart_path="chart",
            chart_version="0.1.0",
            runner=self.run,
            temporary_directory=tmp_path,
        )

    async def run(self, argv, environment):
        assert environment == {"HELM_DRIVER": "configmap"}
        self.calls.append(argv)
        if argv[1] == "version":
            stdout = "v3.19.4\n"
        elif argv[1:3] == ("get", "values"):
            stdout = json.dumps(self.current)
        elif argv[1] == "history":
            stdout = json.dumps([{"revision": 1, "status": "deployed"}])
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
