"""The real lifecycle dispatcher retains migration identity through final delivery."""

from dataclasses import replace

import pytest
from test_provider_lifecycle import _config, _context, _v2_request

from exomem_provisioner.driver import DriverFinal, DriverPending, DriverTerminal
from exomem_provisioner.governance_migration_checkpoint import MigrationCheckpoint
from exomem_provisioner.lifecycle import CellLifecycleDriver, HighFidelityProviderPlane
from exomem_provisioner.wire_protocol import WIRE_PROTOCOL_V2


def config():
    return replace(
        _config(),
        migration_mode="governance-v3-to-v4",
        compatibility_digest="9" * 64,
        runtime_target=_v2_request()["runtimeTarget"],
    )


class MigrationPlane(HighFidelityProviderPlane):
    def __init__(self):
        super().__init__(location="fsn1")
        self.migration_calls = []
        self.result = None

    async def governance_rollforward(self, metadata, request, context):
        self.migration_calls.append(context)
        return self.result or DriverPending(context.checkpoint, 30)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["inspect", "prepare", "enroll", "commit", "complete", "confirmed"]
)
async def test_dispatch_preserves_governance_checkpoint(phase):
    checkpoint = MigrationCheckpoint(
        phase,
        "a" * 64,
        "A" * 43,
        None if phase == "inspect" else "b" * 64,
        None if phase in {"inspect", "prepare"} else "c" * 64,
    ).encode()
    plane = MigrationPlane()
    driver = CellLifecycleDriver(plane=plane, config=config(), volume_worker=None)
    context = _context(checkpoint=checkpoint, wire_protocol=WIRE_PROTOCOL_V2)
    result = await driver.execute("rollforward", _v2_request(compatibilityDigest="9" * 64), context)
    assert result == DriverPending(checkpoint, 30)
    assert plane.migration_calls == [context]


@pytest.mark.asyncio
async def test_governance_provision_cannot_enter_legacy_admission():
    plane = MigrationPlane()
    driver = CellLifecycleDriver(plane=plane, config=config(), volume_worker=None)
    with pytest.raises(DriverTerminal, match="PROVISIONER_GOVERNANCE_PROVISION_UNAVAILABLE"):
        await driver.execute("provision", _v2_request(), _context(wire_protocol=WIRE_PROTOCOL_V2))


@pytest.mark.asyncio
async def test_governance_final_result_only_follows_plane_completion():
    plane = MigrationPlane()
    plane.result = DriverFinal({"code": "rollforward_preserved"})
    driver = CellLifecycleDriver(plane=plane, config=config(), volume_worker=None)
    assert (
        await driver.execute(
            "rollforward",
            _v2_request(compatibilityDigest="9" * 64),
            _context(wire_protocol=WIRE_PROTOCOL_V2),
        )
        == plane.result
    )
