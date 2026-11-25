"""The committed Claude Code plugin must mirror the packaged sources.

The marketplace installs straight from git, so the plugin's skills/ and hooks/ have
to be committed rather than generated at install time. That creates a second copy of
every skill - exactly the drift hazard this repo keeps hitting. This test is the
guard: edit the scaffold without re-running the sync and CI fails here.

Regenerate with:  exomem package-skills --plugin-root plugins/claude-code
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from exomem import client_config
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
        "plugin tree is out of sync - run: exomem package-skills --plugin-root plugins/claude-code"
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


def test_plugin_never_auto_starts_a_full_stdio_core() -> None:
    manifest = json.loads((PLUGIN_ROOT / _MANIFEST).read_text(encoding="utf-8"))

    server = manifest["mcpServers"]["evermatter"]
    assert server == {"type": "http", "url": "${user_config.mcp_url}"}
    option = manifest["userConfig"]["mcp_url"]
    assert option["type"] == "string"
    assert option["default"] == ""
    assert option["required"] is False
    assert "/mcp" in option["description"]
    assert "command" not in server and "args" not in server


def test_plugin_http_placeholder_passes_claude_validation() -> None:
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("claude CLI is unavailable")

    # The repository root holds the marketplace and its `renames` map; the other path is the plugin.
    for target in (REPO_ROOT, PLUGIN_ROOT):
        result = subprocess.run(
            [claude, "plugin", "validate", str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

        assert result.returncode == 0, f"{target}: {result.stdout}{result.stderr}"


def test_plugin_ships_every_skill() -> None:
    from exomem import workflow_skills

    expected = {"evermatter"} | {str(s["name"]) for s in workflow_skills.list_skills()}
    shipped = {p.name for p in (PLUGIN_ROOT / "skills").iterdir() if p.is_dir()}

    assert shipped == expected
    for name in shipped:
        assert (PLUGIN_ROOT / "skills" / name / "SKILL.md").is_file()


def test_core_skill_does_not_nest_the_workflow_skills() -> None:
    """They ship as siblings; nesting would expose each SKILL.md at two paths."""
    assert not (PLUGIN_ROOT / "skills" / "evermatter" / "workflow-skills").exists()


def test_hooks_file_nests_events_under_a_top_level_hooks_key() -> None:
    """Events at the document root parse as JSON but the plugin refuses to load:
    `expected record, received undefined` at path ["hooks"]. Only installing the
    plugin surfaces that, so pin the shape here."""
    document = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    assert set(document) <= {"description", "hooks"}
    assert isinstance(document.get("hooks"), dict) and document["hooks"]
    # A known event must live under "hooks", not at the root.
    assert "Stop" in document["hooks"]
    assert "Stop" not in document


def test_hook_commands_resolve_through_the_plugin_root_placeholder() -> None:
    document = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    for event, groups in document["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                command = hook["command"]
                assert "${CLAUDE_PLUGIN_ROOT}" in command, event
                # Quoted: the expanded install path routinely contains spaces.
                assert '"${CLAUDE_PLUGIN_ROOT}' in command, event
                script = command.split('"')[1].rsplit("/", 1)[-1]
                assert (PLUGIN_ROOT / "hooks" / script).is_file(), script


@pytest.mark.skipif(
    shutil.which("python3") is None and shutil.which("python") is None,
    reason="the checkpoint wrapper needs python3 or python on PATH",
)
def test_plugin_checkpoint_survives_a_compaction(tmp_path: Path) -> None:
    """Claude Code runs the generated commands verbatim: a checkpoint saved at PreCompact
    must come back as SessionStart context after the compaction."""
    document = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    pre_compact = document["hooks"]["PreCompact"][0]["hooks"][0]["command"]
    session_start = next(
        entry["hooks"][0]["command"]
        for entry in document["hooks"]["SessionStart"]
        if "compact" in entry.get("matcher", "")
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
        "HOME": str(home),
        "USERPROFILE": str(home),
    }

    def run(command: str, payload: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", command],
            input=json.dumps({"session_id": "s1", "cwd": str(tmp_path), **payload}),
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            timeout=60,
        )

    run(pre_compact, {"hook_event_name": "PreCompact", "trigger": "auto"})
    resumed = run(session_start, {"hook_event_name": "SessionStart", "source": "compact"})

    output = json.loads(resumed.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    assert output["additionalContext"], resumed.stderr


def test_marketplace_points_at_the_plugin_directory() -> None:
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))

    entry = next(p for p in marketplace["plugins"] if p["name"] == "evermatter")
    # `source` is relative to the REPO ROOT, not to .claude-plugin/.
    source = (REPO_ROOT / entry["source"]).resolve()
    assert source == PLUGIN_ROOT.resolve()


def test_marketplace_moves_an_installed_exomem_plugin_to_the_evermatter_name() -> None:
    """Claude Code stores the plugin server's sign-in under the plugin and server names.

    The rename signs each user of the plugin's URL out once; the `renames` map carries the
    install and settings across, and the notice hook names the sign-in step.
    """
    manifest = json.loads((PLUGIN_ROOT / _MANIFEST).read_text(encoding="utf-8"))
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))

    assert marketplace["renames"] == {"exomem": "evermatter"}
    assert marketplace["name"] == "exomem"  # the marketplace id stays: users added it by this name
    assert manifest["name"] == "evermatter"
    assert set(manifest["mcpServers"]) == {"evermatter"}


def _run_sign_in_notice(tmp_path: Path, **plugin_env: str) -> str:
    """Run the plugin's `startup` SessionStart command verbatim, as Claude Code does."""
    document = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = next(
        entry["hooks"][0]["command"]
        for entry in document["hooks"]["SessionStart"]
        if entry.get("matcher") == "startup"
    )
    # A test run inside Claude Code inherits its entry point, which would silence the hook.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE_PLUGIN_") and k != "CLAUDE_CODE_ENTRYPOINT"
    }
    env.update({"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), **plugin_env})
    done = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=30
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


needs_python = pytest.mark.skipif(
    shutil.which("python3") is None and shutil.which("python") is None,
    reason="the notice wrapper needs python3 or python on PATH",
)


@needs_python
def test_sign_in_notice_names_the_step_once_and_not_a_second_time(tmp_path: Path) -> None:
    plugin_env = {
        "CLAUDE_PLUGIN_OPTION_MCP_URL": "https://memory.example/mcp",
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
    }

    first = _run_sign_in_notice(tmp_path, **plugin_env)
    second = _run_sign_in_notice(tmp_path, **plugin_env)

    # The hook cannot import the package, so it holds its own copy of the step.
    assert client_config.SIGN_IN_STEPS["claude"] in json.loads(first)["systemMessage"]
    assert second == ""


@needs_python
def test_sign_in_notice_waits_out_a_headless_run_for_a_session_a_person_reads(
    tmp_path: Path,
) -> None:
    plugin_env = {
        "CLAUDE_PLUGIN_OPTION_MCP_URL": "https://memory.example/mcp",
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
    }

    headless = _run_sign_in_notice(tmp_path, CLAUDE_CODE_ENTRYPOINT="sdk-cli", **plugin_env)
    interactive = _run_sign_in_notice(tmp_path, CLAUDE_CODE_ENTRYPOINT="cli", **plugin_env)

    assert headless == ""
    assert "systemMessage" in interactive  # the headless run left no marker behind


@needs_python
def test_sign_in_notice_stays_silent_while_the_plugin_has_no_url_to_sign_in_to(
    tmp_path: Path,
) -> None:
    silent = _run_sign_in_notice(tmp_path, CLAUDE_PLUGIN_DATA=str(tmp_path / "data"))
    later = _run_sign_in_notice(
        tmp_path,
        CLAUDE_PLUGIN_DATA=str(tmp_path / "data"),
        CLAUDE_PLUGIN_OPTION_MCP_URL="https://memory.example/mcp",
    )

    assert silent == ""
    assert "systemMessage" in later  # the silent run left no marker behind


def test_manifest_carries_listing_metadata() -> None:
    """A curated listing shows author, homepage, and licence.

    Without these the plugin lists as anonymous and unlicensed, which reads as
    abandoned next to entries that carry them. This keeps the self-hosted
    listing from silently regressing to bare name, description and version.
    """
    manifest = json.loads((PLUGIN_ROOT / _MANIFEST).read_text(encoding="utf-8"))

    assert manifest["author"]["name"]
    assert manifest["author"]["url"].startswith("https://")
    assert manifest["homepage"].startswith("https://")
    assert manifest["repository"].startswith("https://github.com/")
    assert manifest["license"] == "Apache-2.0"
    assert "memory" in manifest["keywords"]


def test_listing_identity_agrees_across_both_plugin_surfaces() -> None:
    """The two listings describe one product, so they must not contradict.

    A reviewer comparing the self-hosted marketplace entry against the Cloud
    directory entry sees both. Divergent authorship or licence there reads as
    two unrelated projects sharing a name.
    """
    manifest = json.loads((PLUGIN_ROOT / _MANIFEST).read_text(encoding="utf-8"))
    cloud = json.loads(
        (REPO_ROOT / "plugins" / "cloud" / "definition.json").read_text(encoding="utf-8")
    )

    assert manifest["author"]["name"] == cloud["author"]["name"]
    assert manifest["author"]["url"] == cloud["author"]["url"]
    assert manifest["homepage"] == cloud["homepage"]
    assert manifest["repository"] == cloud["repository"]
    assert manifest["license"] == cloud["license"]


def test_plugin_version_matches_the_selected_release() -> None:
    """Marketplace installs must pin the same version as the selected package source."""
    committed = json.loads((PLUGIN_ROOT / _MANIFEST).read_text(encoding="utf-8"))
    assert committed["version"] == package_module.release_version()
