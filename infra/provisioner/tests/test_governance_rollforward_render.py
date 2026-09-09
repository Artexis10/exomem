"""The governance rollforward hands the cell chart only values that chart accepts."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from test_governance_rollforward_live import RollforwardHarness

CELL_CHART = Path(__file__).resolve().parents[3] / "infra/helm/cell"


async def test_actual_rollforward_serving_render_never_carries_the_governance_mode():
    """The migration runs in the coordinator's Job; the cell chart has no governance branch."""

    h = RollforwardHarness()
    assert h.config.migration_mode == "governance-v3-to-v4"
    await h.drained()
    await h.step()

    [values] = h.helm_calls
    assert values["workloadMode"] == "serve"
    assert values["migrationMode"] == "none"

    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("pinned Helm is required for rendered chart acceptance")
    rendered = subprocess.run(
        [helm, "template", h.current.resource_name, str(CELL_CHART), "--values", "-"],
        input=json.dumps(values),
        text=True,
        capture_output=True,
        check=False,
    )

    assert rendered.returncode == 0, rendered.stderr
    documents = [doc for doc in yaml.safe_load_all(rendered.stdout) if isinstance(doc, dict)]
    assert "StatefulSet" in {doc["kind"] for doc in documents}
