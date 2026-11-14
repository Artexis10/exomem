"""TEMPORARY Windows diagnostic, deleted before the change ends: where the offline migrate route stops."""

from __future__ import annotations

import os
import sys
import traceback

import pytest
from test_collection_store_migration import OPERATOR, legacy_vault, stop_writer
from test_vault_store_migration import (
    ALPHA,
    BETA,
    OPERATOR_SECRET,
    RELEASED,
    _lease_environment,
    coordinator_url,  # noqa: F401
)

from exomem.collection_store import admission, capability, custody
from exomem.governance.principal import library_scope

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DIAG_ANY"), reason="Windows diagnostic"
)


def _route(tmp_path, monkeypatch, url, step):
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv(OPERATOR, raising=False)
    monkeypatch.delenv(custody.SYNC_ROOTS_ENV, raising=False)
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    for name, value in _lease_environment(tmp_path, url).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(capability, "RELEASED", RELEASED)
    outcome = None
    with library_scope():
        root = legacy_vault(tmp_path / "vault")
        (root / ALPHA).write_text("---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\nIn [[Beta]].\n")
        (root / BETA).write_text("---\ntitle: Beta\ntype: note\n---\n# Beta\n\nThe table.\n")
        with pytest.MonkeyPatch.context() as shell:
            shell.setenv(OPERATOR, OPERATOR_SECRET)
            try:
                outcome = "OK " + repr(admission.migrate_route(root, step=step))[:1500]
            except BaseException:  # noqa: BLE001 - diagnostic
                outcome = "RAISED\n" + traceback.format_exc()
    stop_writer()
    pytest.fail(f"DIAG {step}: {outcome}")


@pytest.mark.parametrize("step", ["dry-run", "prepare", "apply"])
def test_route(tmp_path, monkeypatch, coordinator_url, step):  # noqa: F811
    _route(tmp_path, monkeypatch, coordinator_url, step)
