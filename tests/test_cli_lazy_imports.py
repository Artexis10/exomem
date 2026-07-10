"""Regression coverage for the CLI's model-free import paths."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VAULT = ROOT / "tests" / "fixtures"
HEAVY_MODULES = ("torch", "sentence_transformers", "fastmcp")


def _run_cli_with_module_probe(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], set[str]]:
    modules_path = tmp_path / "modules.json"
    (tmp_path / "sitecustomize.py").write_text(
        """
import atexit
import json
import os
import sys


def dump_modules():
    with open(os.environ["EXOMEM_MODULES_PATH"], "w", encoding="utf-8") as output:
        json.dump(sorted(sys.modules), output)


atexit.register(dump_modules)
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
            "EXOMEM_MODULES_PATH": str(modules_path),
            "EXOMEM_VAULT_PATH": str(FIXTURE_VAULT),
            "PYTHONPATH": os.pathsep.join(
                part for part in (str(tmp_path), str(ROOT / "src"), env.get("PYTHONPATH")) if part
            ),
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "exomem", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, set(json.loads(modules_path.read_text(encoding="utf-8")))


def _assert_no_heavy_modules(modules: set[str]) -> None:
    for module in HEAVY_MODULES:
        assert module not in modules
        assert not any(name.startswith(f"{module}.") for name in modules)
    assert "exomem.server" not in modules


def test_help_does_not_import_heavy_stacks(tmp_path: Path) -> None:
    result, modules = _run_cli_with_module_probe(tmp_path, "--help")

    assert result.returncode == 0, result.stderr
    assert "MCP transport to serve" in result.stdout
    _assert_no_heavy_modules(modules)


def test_status_one_shot_does_not_import_heavy_stacks(tmp_path: Path) -> None:
    result, modules = _run_cli_with_module_probe(tmp_path, "status", "--json", "--vault", str(FIXTURE_VAULT))

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["models"]["module_loaded"] is False
    _assert_no_heavy_modules(modules)
