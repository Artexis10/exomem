"""The committed Claude Code plugin must mirror the packaged sources.

The marketplace installs straight from git, so the plugin's skills/ and hooks/ have
to be committed rather than generated at install time. That creates a second copy of
every skill - exactly the drift hazard this repo keeps hitting. This test is the
guard: edit the scaffold without re-running the sync and CI fails here.

Regenerate with:  exomem package-skills --plugin-root plugins/claude-code
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import package_skills as package_module

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "claude-code"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"

# plugin.json carries the package version, which legitimately differs between a
# checkout and a release build; compare it structurally instead of byte-for-byte.
_MANIFEST = ".claude-plugin/plugin.json"


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


@pytest.fixture(scope="module")
def regenerated(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    out = tmp_path_factory.mktemp("plugin")
    package_module.sync_plugin(out)
    return _tree(out)


def test_committed_plugin_matches_the_packaged_sources(regenerated: dict[str, bytes]) -> None:
    committed = _tree(PLUGIN_ROOT)

    assert set(committed) == set(regenerated), (
        "plugin tree is out of sync - run: "
        "exomem package-skills --plugin-root plugins/claude-code"
    )
    drifted = [
        name
        for name, content in regenerated.items()
        if name != _MANIFEST and committed[name] != content
    ]
    assert not drifted, (
        f"these plugin files drifted from the scaffold: {drifted}. "
        "Regenerate with: exomem package-skills --plugin-root plugins/claude-code"
    )


def test_manifest_matches_apart_from_the_version(regenerated: dict[str, bytes]) -> None:
    committed = json.loads((PLUGIN_ROOT / _MANIFEST).read_text(encoding="utf-8"))
    expected = json.loads(regenerated[_MANIFEST].decode("utf-8"))

    committed.pop("version", None)
    expected.pop("version", None)
    assert committed == expected


def test_plugin_declares_stdio_transport_explicitly() -> None:
    """The server defaults to http; omitting --transport stdio starts a web server."""
    manifest = json.loads((PLUGIN_ROOT / _MANIFEST).read_text(encoding="utf-8"))

    args = manifest["mcpServers"]["exomem"]["args"]
    assert "--transport" in args
    assert args[args.index("--transport") + 1] == "stdio"


def test_plugin_ships_every_skill() -> None:
    from exomem import workflow_skills

    expected = {"exomem"} | {str(s["name"]) for s in workflow_skills.list_skills()}
    shipped = {p.name for p in (PLUGIN_ROOT / "skills").iterdir() if p.is_dir()}

    assert shipped == expected
    for name in shipped:
        assert (PLUGIN_ROOT / "skills" / name / "SKILL.md").is_file()


def test_core_skill_does_not_nest_the_workflow_skills() -> None:
    """They ship as siblings; nesting would expose each SKILL.md at two paths."""
    assert not (PLUGIN_ROOT / "skills" / "exomem" / "workflow-skills").exists()


def test_hook_commands_resolve_through_the_plugin_root_placeholder() -> None:
    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    for event, groups in hooks.items():
        for group in groups:
            for hook in group["hooks"]:
                assert "${CLAUDE_PLUGIN_ROOT}" in hook["command"], event
                script = hook["command"].rsplit("/", 1)[-1]
                assert (PLUGIN_ROOT / "hooks" / script).is_file(), script


def test_marketplace_points_at_the_plugin_directory() -> None:
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))

    entry = next(p for p in marketplace["plugins"] if p["name"] == "exomem")
    # `source` is relative to the REPO ROOT, not to .claude-plugin/.
    source = (REPO_ROOT / entry["source"]).resolve()
    assert source == PLUGIN_ROOT.resolve()
