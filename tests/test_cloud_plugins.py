from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import textwrap
import tomllib
import zipfile
from pathlib import Path

import pytest

from exomem import cloud_plugins, package_skills, workflow_skills
from exomem.public_artifact_privacy import assert_public_artifacts_clean

ROOT = Path(__file__).resolve().parents[1]


def test_directory_starters_ship_from_the_shared_product_definition(tmp_path: Path) -> None:
    # Catch a generator dropping the public starters while its source looks complete.
    cloud_plugins.build_packages(ROOT, tmp_path)
    definition = json.loads((ROOT / "plugins/cloud/definition.json").read_text())
    manifest = json.loads((tmp_path / "openai/plugin.json").read_text())
    interface = manifest["extensions"]["com.openai"]["interface"]
    assert interface["shortDescription"] == definition["short_description"]
    assert interface["longDescription"] == definition["description"]
    assert manifest["extensions"]["com.openai"]["publication"]["release_notes"] == (
        definition["release_notes"]["openai"]
    )
    assert (
        manifest["extensions"]["com.openai"]["interface"]["defaultPrompt"]
        == definition["default_prompts"]
    )
    for provider in ("claude", "openai"):
        readme = (tmp_path / provider / "README.md").read_text()
        for prompt in definition["default_prompts"]:
            assert prompt in readme
        assert definition["description"] in readme
        assert definition["release_notes"][provider] in readme


def test_claude_directory_manifest_declares_privacy_policy(tmp_path: Path) -> None:
    cloud_plugins.build_packages(ROOT, tmp_path)
    definition = json.loads((ROOT / "plugins/cloud/definition.json").read_text())
    with zipfile.ZipFile(tmp_path / "claude.zip") as archive:
        manifest = json.loads(archive.read(".claude-plugin/plugin.json"))
        assert manifest["privacyPolicyUrl"] == definition["privacy"]


def test_claude_directory_links_and_icon_are_complete(tmp_path: Path) -> None:
    cloud_plugins.build_packages(ROOT, tmp_path)
    definition = json.loads((ROOT / "plugins/cloud/definition.json").read_text())
    with zipfile.ZipFile(tmp_path / "claude.zip") as archive:
        manifest = json.loads(archive.read(".claude-plugin/plugin.json"))
        assert manifest["displayName"] == definition["display_name"]
        for field, key in (
            ("privacyPolicyUrl", "privacy"),
            ("supportUrl", "support"),
            ("documentationUrl", "documentation"),
            ("termsOfServiceUrl", "terms"),
        ):
            assert manifest[field] == definition[key]
        # Claude's directory takes a square PNG or JPEG of 512 to 2048 px under 2 MB, not SVG.
        relative = manifest["icon"].removeprefix("./")
        data = archive.read(relative)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert width == height and 512 <= width <= 2048
    assert len(data) < 2 * 1024 * 1024
    assert data == (ROOT / "plugins/cloud" / relative).read_bytes()


def test_public_readmes_explain_data_handling_and_all_product_links(tmp_path: Path) -> None:
    cloud_plugins.build_packages(ROOT, tmp_path)
    definition = json.loads((ROOT / "plugins/cloud/definition.json").read_text())
    for provider in ("claude", "openai"):
        readme = (tmp_path / provider / "README.md").read_text()
        for key in ("privacy", "terms", "support", "documentation"):
            assert definition[key] in readme
        assert "Data handling" in readme
        assert "OAuth" in readme
        assert "recall_context" in readme
        assert "hooks" in readme.lower()


def test_claude_readme_discloses_content_free_hook_observability(tmp_path: Path) -> None:
    cloud_plugins.build_packages(ROOT, tmp_path)
    readme = (tmp_path / "claude/README.md").read_text()
    assert "Hook logs contain metadata only" in readme
    assert "not prompt or assistant snippets" in readme


def test_claude_cloud_hooks_use_canonical_bytes_and_native_mcp_binding(tmp_path: Path) -> None:
    cloud_plugins.build_packages(ROOT, tmp_path)
    with zipfile.ZipFile(tmp_path / "claude.zip") as archive:
        manifest = json.loads(archive.read(".claude-plugin/plugin.json"))
        # Default hooks/hooks.json is loaded once, never also declared inline.
        assert "hooks" not in manifest
        hooks = json.loads(archive.read("hooks/hooks.json"))["hooks"]
        # Unused shell launchers contain computed paths that block directory validation.
        assert not any(
            name.startswith("hooks/") and name.endswith(".sh") for name in archive.namelist()
        )
        assert set(hooks) == {
            "UserPromptSubmit",
            "Stop",
            "PreCompact",
            "SessionEnd",
            "SessionStart",
        }
        for event, groups in hooks.items():
            for group in groups:
                for handler in group["hooks"]:
                    assert handler["command"] == "python3"
                    args = handler["args"]
                    script = args[0].removeprefix("${CLAUDE_PLUGIN_ROOT}/")
                    assert script.endswith(".py")
                    assert (
                        archive.read(script)
                        == (ROOT / "src/exomem/_hooks" / Path(script).name).read_bytes()
                    )
                    assert args[1:5] == [
                        "--client",
                        "claude",
                        "--hook-home-env",
                        "CLAUDE_PLUGIN_DATA",
                    ]
                    if event in {"Stop", "UserPromptSubmit"}:
                        assert args[5:] == ["--activation-mode", "mcp"]
        for filename in (
            "exomem_retrieve_nudge.py",
            "exomem_capture_nudge.py",
            "exomem_continuation_checkpoint.py",
        ):
            assert (
                archive.read("hooks/" + filename)
                == (ROOT / "src/exomem/_hooks" / filename).read_bytes()
            )
    with zipfile.ZipFile(tmp_path / "openai.zip") as archive:
        assert not any(name.startswith("hooks/") for name in archive.namelist())


def test_registered_cloud_hooks_do_not_read_or_forward_local_credentials(tmp_path: Path) -> None:
    # Catch a public archive carrying local credential access, or losing its
    # native activation/capture behaviour when that local-only dependency is absent.
    cloud_plugins.build_packages(ROOT, tmp_path / "built")
    package = tmp_path / "unpacked"
    with zipfile.ZipFile(tmp_path / "built/claude.zip") as archive:
        assert "hooks/exomem_local_credentials.py" not in archive.namelist()
        for name in archive.namelist():
            if name.startswith("hooks/") and name.endswith(".py"):
                assert b"EXOMEM_REST_API_KEY" not in archive.read(name), name
        archive.extractall(package)
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("EXOMEM_", "KB_", "CLAUDE_", "CODEX_"))
    }
    env.update(
        {
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "CLAUDE_PLUGIN_DATA": str(tmp_path / "state"),
            "EXOMEM_PROMINENCE": "maximal",
            "EXOMEM_CONFIG_PATH": str(tmp_path / "absent-config.json"),
            "EXOMEM_REST_API_KEY": "synthetic-must-not-be-read",
            "EXOMEM_RETRIEVE_INJECT": "working_set",
            "KB_RETRIEVE_INJECT": "1",
            "EXOMEM_RETRIEVE_INJECT_CLI": "1",
            "EXOMEM_HOST": "http://127.0.0.1:9",
            "EXOMEM_CAPTURE_NUDGE_MIN_CHARS": "1",
            "EXOMEM_EPISODE_ASK_TURNS": "1",
        }
    )
    secret = tmp_path / "local-token"
    secret.write_text("synthetic-must-not-be-read")
    env["EXOMEM_LOCAL_TOKEN_FILE"] = str(secret)
    guard = tmp_path / "guard"
    guard.mkdir()
    attempts = tmp_path / "credential-attempts.log"
    env.update(PYTHONPATH=str(guard), CHECK_SECRET=str(secret), CHECK_ATTEMPTS=str(attempts))
    # Hooks soft-fail, so retain attempted operations even if the hook catches the refusal.
    (guard / "sitecustomize.py").write_text(
        textwrap.dedent(
            """
            import os
            import sys
            from pathlib import Path

            secret = os.environ['CHECK_SECRET']
            log = os.open(os.environ['CHECK_ATTEMPTS'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

            def audit(event, args):
                forbidden = event == 'socket.connect' or event == 'os.system'
                if event == 'open':
                    forbidden = str(args[0]) == secret
                if event == 'subprocess.Popen':
                    executable, argv, cwd, env = args
                    forbidden = Path(executable).name.lower() not in {'git', 'git.exe'}
                    child_env = os.environ if env is None else env
                    forbidden |= any('synthetic-must-not-be-read' in str(v) for v in child_env.values())
                if forbidden:
                    os.write(log, (event + '\\n').encode())
                    raise RuntimeError('unexpected local credential or transport operation')

            sys.addaudithook(audit)
            """
        )
    )
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "Review the adapter."}})
        + "\n"
        + json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": "The adapter is ready."}}
        )
        + "\n"
    )
    registrations = json.loads((package / "hooks/hooks.json").read_text())["hooks"]
    for event, fields, expected in (
        ("UserPromptSubmit", {"prompt": "continue"}, "recall_context"),
        (
            "Stop",
            {"last_assistant_message": "We settled the adapter boundary."},
            "Exomem capture check",
        ),
        ("PreCompact", {"trigger": "manual"}, ""),
        ("SessionEnd", {}, ""),
        ("SessionStart", {"source": "resume"}, "[Exomem continuation checkpoint]"),
    ):
        handler = registrations[event][0]["hooks"][0]
        argv = [arg.replace("${CLAUDE_PLUGIN_ROOT}", str(package)) for arg in handler["args"]]
        result = subprocess.run(
            [sys.executable, *argv],
            input=json.dumps(
                {
                    "session_id": "packaged-native",
                    "hook_event_name": event,
                    "cwd": str(tmp_path),
                    "transcript_path": str(transcript),
                    **fields,
                }
            ),
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert attempts.exists(), "The isolated process audit did not run"
        assert not attempts.read_text(), (event, attempts.read_text())
        assert expected in result.stdout
        assert "synthetic-must-not-be-read" not in result.stdout + result.stderr


@pytest.mark.parametrize("placement", ["root", "extension", "file", "compatibility"])
def test_openai_public_submission_rejects_lifecycle_hooks(tmp_path: Path, placement: str) -> None:
    cloud_plugins.build_packages(ROOT, tmp_path)
    package = tmp_path / "openai"
    manifest_path = package / "plugin.json"
    manifest = json.loads(manifest_path.read_text())
    if placement == "file":
        (package / "hooks").mkdir()
        (package / "hooks/hooks.json").write_text('{"hooks":{}}')
    elif placement == "compatibility":
        (package / ".codex-plugin").mkdir()
        (package / ".codex-plugin/plugin.json").write_text('{"hooks":{"Stop":[]}}')
    else:
        target = manifest if placement == "root" else manifest["extensions"]["com.openai"]
        target["hooks"] = {"UserPromptSubmit": []}
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="lifecycle hooks"):
        cloud_plugins._validate_openai(ROOT, package)


@pytest.mark.parametrize("placement", ["extension", "file", "compatibility"])
def test_openai_public_submission_rejects_app_references(tmp_path: Path, placement: str) -> None:
    cloud_plugins.build_packages(ROOT, tmp_path)
    package = tmp_path / "openai"
    if placement == "file":
        (package / ".app.json").write_text('{"apps":{"sample":"id"}}')
    elif placement == "compatibility":
        (package / ".codex-plugin").mkdir()
        (package / ".codex-plugin/plugin.json").write_text('{"apps":{"sample":"id"}}')
    else:
        path = package / "plugin.json"
        manifest = json.loads(path.read_text())
        manifest["extensions"]["com.openai"]["apps"] = {"sample": "id"}
        path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="app references"):
        cloud_plugins._validate_openai(ROOT, package)


def test_openai_submission_icons_are_contained_square_pngs(tmp_path: Path) -> None:
    cloud_plugins.build_packages(ROOT, tmp_path)
    with zipfile.ZipFile(tmp_path / "openai.zip") as archive:
        manifest = json.loads(archive.read("plugin.json"))
        interface = manifest["extensions"]["com.openai"]["interface"]
        for key, minimum in (("logo", 256), ("composerIcon", 48)):
            relative = interface[key].removeprefix("./")
            assert relative == ("assets/icon.png" if key == "logo" else "assets/icon-small.png")
            data = archive.read(relative)
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            width, height = struct.unpack(">II", data[16:24])
            assert width == height and minimum <= width <= 4096
            assert len(data) <= 5 * 1024 * 1024
            assert data == (ROOT / "plugins/cloud" / relative).read_bytes()


def test_cloud_packages_share_complete_canonical_skill_bytes(tmp_path: Path) -> None:
    release = cloud_plugins.build_packages(ROOT, tmp_path)

    assert (
        release["version"]
        == tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    )
    assert release["skill_contract"] == workflow_skills.skill_contract()
    assert (
        release["corpus_digest"]
        == hashlib.sha256((ROOT / "plugins/cloud/evals/cases.json").read_bytes()).hexdigest()
    )
    assert release["profile"] == "product-cloud"
    for name in ("exomem", *(str(item["name"]) for item in workflow_skills.list_skills())):
        payload = package_skills.skill_payload(name)
        for provider in ("claude", "openai"):
            for relative, content in payload.items():
                assert (
                    tmp_path / provider / "skills" / name / relative
                ).read_bytes() == content.encode()
    for provider in ("claude", "openai"):
        with zipfile.ZipFile(tmp_path / f"{provider}.zip") as archive:
            assert all(
                not name.startswith("/") and ".." not in Path(name).parts
                for name in archive.namelist()
            )
            assert (
                archive.read("skills/exomem-capture/references/writing.md")
                == (
                    tmp_path / provider / "skills/exomem-capture/references/writing.md"
                ).read_bytes()
            )


def test_cloud_packages_are_deterministic_and_drift_is_rejected(tmp_path: Path) -> None:
    cloud_plugins.build_packages(ROOT, tmp_path)
    first = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    cloud_plugins.build_packages(ROOT, tmp_path)
    assert first == {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert cloud_plugins.check_packages(ROOT, tmp_path) == {"ok": True, "issues": []}

    (tmp_path / "openai/.app.json").write_text('{"apps":{"private":"id"}}')
    assert "extra: openai/.app.json" in cloud_plugins.check_packages(ROOT, tmp_path)["issues"]
    (tmp_path / "openai/plugin.json").write_text('{"name":"bad"}')
    assert "drift: openai/plugin.json" in cloud_plugins.check_packages(ROOT, tmp_path)["issues"]


def test_cloud_package_check_rejects_symlinks_and_extra_directories(tmp_path: Path) -> None:
    cloud_plugins.build_packages(ROOT, tmp_path)
    (tmp_path / "openai/empty-extra").mkdir()
    (tmp_path / "openai/linked-file").symlink_to("README.md")

    issues = cloud_plugins.check_packages(ROOT, tmp_path)["issues"]
    assert "extra: openai/empty-extra/" in issues
    assert "symlink: openai/linked-file" in issues


def test_cloud_package_check_reports_missing_corpus(tmp_path: Path) -> None:
    root = tmp_path / "source"
    (root / "plugins/cloud").mkdir(parents=True)
    (root / "plugins/cloud/definition.json").write_bytes(
        (ROOT / "plugins/cloud/definition.json").read_bytes()
    )
    (root / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())

    result = cloud_plugins.check_packages(root, tmp_path / "output")

    assert result["ok"] is False
    assert any("cases.json" in issue for issue in result["issues"])


def test_cloud_openai_schema_and_public_archives(tmp_path: Path) -> None:
    cloud_plugins.build_packages(ROOT, tmp_path)
    manifest = json.loads((tmp_path / "openai/plugin.json").read_text())
    assert set(manifest["extensions"]) == {"com.openai"}
    assert "apps" not in manifest["extensions"]["com.openai"]
    extension = manifest["extensions"]["com.openai"]
    assert len(extension["review"]["test_cases"]["positive"]) == 5
    assert len(extension["review"]["test_cases"]["negative"]) == 3
    assert extension["review"]["commerce"] is False
    assert extension["publication"]["countries"] == []
    assert extension["publication"]["release_notes"].strip()
    assert json.loads((tmp_path / "openai/mcp.json").read_text())["mcpServers"] == {
        "exomem": {"type": "streamable-http", "url": "https://exomem.substratesystems.io/mcp"}
    }
    archives = [tmp_path / "claude.zip", tmp_path / "openai.zip"]
    assert_public_artifacts_clean(
        archives,
        labels={path: f"plugins/cloud/generated/{path.name}" for path in archives},
    )
