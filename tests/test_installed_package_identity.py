"""Older direct runtimes retain their own identity when the controller reads it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_controller_reads_older_installed_target_without_the_new_helper(tmp_path):
    site = tmp_path / "installed-site"
    package = site / "exomem"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = '0.0.1'\n")
    (package / "install_info.py").write_text('"""Older package without the controller helper."""\n')
    metadata = site / "exomem-0.0.1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: exomem\nVersion: 0.0.1\n")
    shim = Path(__file__).resolve().parents[1] / "scripts/installed_package_identity.py"
    command = (
        "import importlib.util,json,sys; "
        "spec=importlib.util.spec_from_file_location('controller_identity',sys.argv[1]); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "print(json.dumps(module.package_identity('installed',role='observer-and-direct-runtime')))"
    )
    observed = subprocess.run(
        [sys.executable, "-c", command, str(shim)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(site)},
        capture_output=True,
        text=True,
    )
    assert observed.returncode == 0, observed.stderr
    identity = json.loads(observed.stdout)
    assert identity["version"] == identity["distribution_version"] == "0.0.1"
    assert identity["origin"] == "non-editable-distribution"
    assert identity["source_revision"] is None
    assert identity["role"] == "observer-and-direct-runtime"
    assert str(site) not in observed.stdout + observed.stderr
