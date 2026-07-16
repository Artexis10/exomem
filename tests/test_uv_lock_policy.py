from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UV_VERSION = "0.11.28"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_release_please_keeps_root_package_version_locked() -> None:
    project = tomllib.loads(_read("pyproject.toml"))
    project_version = project["project"]["version"]
    release_config = json.loads(_read("release-please-config.json"))
    extra_files = release_config["packages"]["."]["extra-files"]

    assert {"type": "generic", "path": "uv.lock"} in extra_files

    match = re.search(
        r'\[\[package\]\]\nname = "exomem"\n'
        r'version = "(?P<version>[^"]+)"(?P<suffix>[^\n]*)',
        _read("uv.lock"),
    )
    assert match is not None
    assert match.group("version") == project_version
    assert "x-release-please-version" in match.group("suffix")


def test_uv_writer_version_is_pinned_across_repository_surfaces() -> None:
    project = tomllib.loads(_read("pyproject.toml"))

    assert project["tool"]["uv"]["required-version"] == f"=={UV_VERSION}"
    assert f"ARG UV_VERSION={UV_VERSION}" in _read("Dockerfile")
    assert f"UV_VERSION={UV_VERSION}" in _read("infra/tool-versions.env")
    assert f'UV_VERSION: "{UV_VERSION}"' in _read(
        ".github/workflows/hosted-infrastructure.yml"
    )


def test_ci_checks_and_never_rewrites_the_root_lockfile() -> None:
    ci = _read(".github/workflows/ci.yml")
    assert "run: uv lock --check" in ci

    for workflow in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if re.search(r"\buv run\b", line):
                assert "uv run --frozen" in line, (
                    f"{workflow.relative_to(ROOT)}:{line_number} may rewrite uv.lock"
                )
