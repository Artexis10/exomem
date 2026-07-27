"""The release branch must be able to resync the hosted definition's version."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sync_hosted_release.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-please.yml"


def load_module():
    spec = importlib.util.spec_from_file_location("sync_hosted_release_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_repo(tmp_path: Path, *, package: str, source: str) -> Path:
    (tmp_path / "plugins" / "hosted").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "exomem"\nversion = "{package}"\n', encoding="utf-8"
    )
    (tmp_path / "plugins" / "hosted" / "definition.json").write_text(
        '{\n  "plugin_id": "exomem-hosted",\n'
        f'  "source_release": "{source}",\n'
        '  "version": "0.1.0"\n}\n',
        encoding="utf-8",
    )
    return tmp_path


def test_sync_rewrites_a_stale_source_release(tmp_path: Path) -> None:
    root = make_repo(tmp_path, package="0.34.0", source="0.33.0")
    result = load_module().sync(root)

    assert (result.previous, result.version, result.changed) == ("0.33.0", "0.34.0", True)
    text = (root / "plugins/hosted/definition.json").read_text(encoding="utf-8")
    assert '"source_release": "0.34.0"' in text
    # Formatting survives, so the generator still owns the file's sha256.
    assert text.startswith('{\n  "plugin_id"')
    assert '"version": "0.1.0"' in text


def test_sync_is_idempotent(tmp_path: Path) -> None:
    root = make_repo(tmp_path, package="0.34.0", source="0.34.0")
    definition = root / "plugins/hosted/definition.json"
    before = definition.read_text(encoding="utf-8")

    result = load_module().sync(root)

    assert result.changed is False
    assert definition.read_text(encoding="utf-8") == before


def test_sync_leaves_the_plugin_version_alone(tmp_path: Path) -> None:
    """`version` is the plugin's own version and must not track the release."""

    root = make_repo(tmp_path, package="0.34.0", source="0.33.0")
    load_module().sync(root)

    text = (root / "plugins/hosted/definition.json").read_text(encoding="utf-8")
    assert '"version": "0.1.0"' in text


def test_check_mode_reports_drift_without_writing(tmp_path: Path) -> None:
    root = make_repo(tmp_path, package="0.34.0", source="0.33.0")
    definition = root / "plugins/hosted/definition.json"
    before = definition.read_text(encoding="utf-8")
    module = load_module()

    assert module.main(["--repo-root", str(root), "--check"]) == 1
    assert definition.read_text(encoding="utf-8") == before

    assert module.main(["--repo-root", str(root)]) == 0
    assert module.main(["--repo-root", str(root), "--check"]) == 0


def test_missing_key_is_an_explicit_failure(tmp_path: Path) -> None:
    root = make_repo(tmp_path, package="0.34.0", source="0.33.0")
    (root / "plugins/hosted/definition.json").write_text('{"plugin_id": "x"}\n', encoding="utf-8")

    with pytest.raises(SystemExit, match="no source_release key"):
        load_module().sync(root)


def test_the_checked_in_repo_is_in_sync() -> None:
    """A release that forgets the resync must not reach main silently."""

    assert load_module().main(["--repo-root", str(REPO_ROOT), "--check"]) == 0


def test_the_release_workflow_runs_the_resync_before_regenerating() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    sync_at = text.find("scripts/sync_hosted_release.py")
    regenerate_at = text.find("hosted-plugin.py regenerate")

    assert sync_at != -1, "the release workflow must resync source_release"
    assert regenerate_at != -1, "the release workflow must regenerate hosted artifacts"
    # regenerate refuses to run while the definition is stale, so order matters.
    assert sync_at < regenerate_at
