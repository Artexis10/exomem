from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from exomem import hosted_plugins

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTERED_OPENAI_APP_ID = "plugin_asdk_app_6a5e3d26f2b08191a04424d1c1b33fc0"
FIXTURE_OPENAI_APP_ID = "plugin_asdk_app_releaseinput123"


def copy_hosted_tree(destination: Path) -> Path:
    shutil.copytree(
        REPO_ROOT / "plugins" / "hosted",
        destination / "plugins" / "hosted",
        ignore=shutil.ignore_patterns("tmp*", ".exomem-hosted-render-*"),
    )
    return destination


def test_openai_candidate_requires_registered_app_release_input(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="registered OpenAI app"):
        hosted_plugins.render(
            REPO_ROOT, tmp_path / "generated", platform="openai", staging_root=tmp_path
        )


@pytest.mark.parametrize("app_id", ["asdk_app_public123", "plugin_asdk_app_", "placeholder"])
def test_openai_candidate_rejects_legacy_or_malformed_registered_app_id(
    tmp_path: Path, app_id: str
) -> None:
    with pytest.raises(ValueError, match="registered OpenAI app"):
        hosted_plugins.render(
            REPO_ROOT,
            tmp_path / "generated",
            platform="openai",
            openai_app_id=app_id,
            staging_root=tmp_path,
        )


def test_openai_candidate_uses_the_registered_plugin_technical_id() -> None:
    app_id = "plugin_asdk_app_public123"

    files = hosted_plugins.candidate_files(
        REPO_ROOT,
        platform="openai",
        openai_app_id=app_id,
    )

    app = json.loads(files["openai/.app.json"])
    assert app["apps"]["exomem"]["id"] == app_id


def test_repository_openai_artifacts_bind_the_registered_technical_id() -> None:
    generated = REPO_ROOT / "plugins/hosted/generated"
    app = json.loads((generated / "openai/.app.json").read_text(encoding="utf-8"))

    assert app["apps"]["exomem"]["id"] == REGISTERED_OPENAI_APP_ID
    with zipfile.ZipFile(generated / "openai.zip") as archive:
        packaged_app = json.loads(archive.read(".app.json"))
    assert packaged_app["apps"]["exomem"]["id"] == REGISTERED_OPENAI_APP_ID
    assert FIXTURE_OPENAI_APP_ID.encode("utf-8") not in (generated / "openai.zip").read_bytes()


def test_repository_openai_identity_rejects_fixture_app_id() -> None:
    with pytest.raises(ValueError, match="fixture"):
        hosted_plugins._validate_repository_openai_app_id(
            REPO_ROOT, FIXTURE_OPENAI_APP_ID
        )


def test_claude_candidate_can_render_and_check_without_openai_registration(tmp_path: Path) -> None:
    rendered = hosted_plugins.render(
        REPO_ROOT, tmp_path / "generated", platform="claude", staging_root=tmp_path
    )

    assert (rendered / "claude/.claude-plugin/plugin.json").is_file()
    assert not (rendered / "openai").exists()


def test_candidate_file_map_is_deterministic_without_staging_directory() -> None:
    first = hosted_plugins.candidate_files(REPO_ROOT, platform="claude")
    second = hosted_plugins.candidate_files(REPO_ROOT, platform="claude")

    assert first == second
    assert "claude/.claude-plugin/plugin.json" in first
    assert "claude.zip" in first


def test_lifecycle_candidate_is_additive_and_binds_records_reader_v2() -> None:
    v1 = hosted_plugins.candidate_files(REPO_ROOT, platform="claude")
    v2 = hosted_plugins.candidate_files(
        REPO_ROOT, platform="claude", candidate="hosted-alpha-agent-v2"
    )

    assert v1["claude/.claude-plugin/plugin.json"] == (
        REPO_ROOT / "plugins/hosted/generated/claude/.claude-plugin/plugin.json"
    ).read_bytes()
    lock = json.loads(v2["claude.lock.json"])
    compatibility = json.loads(v2["compatibility.json"])
    assert lock["profile"] == "hosted-alpha-agent-v2"
    assert lock["minimum_records_reader_version"] == 2
    assert compatibility["commands"][:-1] == json.loads(v1["compatibility.json"])["commands"]
    assert compatibility["commands"][-1] == "record_memory"


def test_lifecycle_source_privacy_gate_refuses_before_rendering_output(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    source = root / "plugins/hosted/candidates/hosted-alpha-agent-v2/selection-cases.json"
    source.write_text('{"api_secret":"private"}', encoding="utf-8")
    output = tmp_path / "rendered"

    with pytest.raises(ValueError, match="credential value"):
        hosted_plugins.render(
            root,
            output,
            platform="claude",
            candidate=hosted_plugins.LIFECYCLE_CANDIDATE,
            staging_root=tmp_path,
        )

    assert not output.exists()


def test_v1_hosted_release_identity_fixture_remains_immutable() -> None:
    fixture = json.loads(
        (REPO_ROOT / "tests/fixtures/hosted/v1-release-identities.json").read_text(
            encoding="utf-8"
        )
    )
    for relative, expected in fixture.items():
        assert hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest() == expected


def test_openai_locks_bind_but_do_not_expose_the_registered_app_id() -> None:
    first_id = "plugin_asdk_app_releaseinput123"
    second_id = "plugin_asdk_app_otherrelease456"
    first = hosted_plugins.candidate_files(
        REPO_ROOT, platform="openai", openai_app_id=first_id
    )
    second = hosted_plugins.candidate_files(
        REPO_ROOT, platform="openai", openai_app_id=second_id
    )

    first_lock = json.loads(first["openai.lock.json"])
    first_archive_lock = json.loads(first["openai.zip.lock.json"])
    expected = hosted_plugins._sha256(first_id.encode("utf-8"))

    assert first_lock["registered_app_id_sha256"] == expected
    assert first_archive_lock["registered_app_id_sha256"] == expected
    assert first_lock != json.loads(second["openai.lock.json"])
    assert first_archive_lock != json.loads(second["openai.zip.lock.json"])
    assert first_id not in first["openai.lock.json"].decode("utf-8")
    assert first_id not in first["openai.zip.lock.json"].decode("utf-8")


def test_openai_check_rejects_a_lock_reused_for_another_registered_app(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.render(
        root,
        openai_app_id="plugin_asdk_app_releaseinput123",
        platform="openai",
    )
    app = root / "plugins/hosted/generated/openai/.app.json"
    app.write_text(
        json.dumps(
            {
                "apps": {
                    "exomem": {
                        "id": "plugin_asdk_app_otherrelease456",
                        "category": "productivity",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not bind the registered app identity"):
        hosted_plugins.check(root, platform="openai")


def test_managed_regeneration_atomically_replaces_existing_candidate(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    stale = root / "plugins/hosted/generated/claude/stale.txt"
    stale.write_text("stale", encoding="utf-8")

    hosted_plugins.regenerate_claude(root)

    assert not stale.exists()
    assert not list((root / "plugins/hosted").glob(".exomem-hosted-render-*"))
    hosted_plugins.check(root, platform="claude")


def test_managed_regeneration_is_serialized_with_promotion(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    release_lock = root / "plugins/hosted/.claude.promotion.lock"
    release_lock.write_text("promotion in progress", encoding="utf-8")

    with pytest.raises(ValueError, match="another process"):
        hosted_plugins.regenerate_claude(root)

    assert release_lock.read_text(encoding="utf-8") == "promotion in progress"


def test_check_recomputes_zip_from_committed_package(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    archive = root / "plugins/hosted/generated/claude.zip"
    with zipfile.ZipFile(archive, "a") as package:
        package.comment = b"tampered"
    lock_path = root / "plugins/hosted/generated/claude.zip.lock.json"
    lock_path.write_text(
        json.dumps(
            {"platform": "claude", "archive_sha256": hosted_plugins._sha256(archive.read_bytes())}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stale"):
        hosted_plugins.check(root, platform="claude")


def test_stale_compatibility_reports_bounded_difference_paths(tmp_path: Path) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    path = root / "plugins/hosted/generated/compatibility.json"
    compatibility = json.loads(path.read_text(encoding="utf-8"))
    compatibility["agent_contract"]["commands"][0]["name"] = "changed"
    path.write_text(json.dumps(compatibility), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"agent_contract\.commands\[0\]\.name",
    ):
        hosted_plugins.check_compatibility_descriptor(root)


def test_claude_archive_is_deterministic_and_locked(tmp_path: Path) -> None:
    first = hosted_plugins.archive(REPO_ROOT, tmp_path / "first", platform="claude")
    second = hosted_plugins.archive(REPO_ROOT, tmp_path / "second", platform="claude")

    assert (first / "claude.zip").read_bytes() == (second / "claude.zip").read_bytes()
    lock = json.loads((first / "claude.zip.lock.json").read_text(encoding="utf-8"))
    assert lock == {
        "platform": "claude",
        "archive_sha256": hosted_plugins._sha256((first / "claude.zip").read_bytes()),
    }
    with zipfile.ZipFile(first / "claude.zip") as package:
        assert {entry.create_system for entry in package.infolist()} == {3}


def test_openai_archive_blocks_an_interleaved_render(tmp_path: Path, monkeypatch) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    first_id = "plugin_asdk_app_releaseinput123"
    hosted_plugins.render(root, openai_app_id=first_id, platform="openai")
    original_archive = hosted_plugins._archive_bytes

    def archive_with_interleaved_render(package: Path) -> bytes:
        with pytest.raises(ValueError, match="another process"):
            hosted_plugins.render(
                root,
                openai_app_id="plugin_asdk_app_otherrelease456",
                platform="openai",
            )
        return original_archive(package)

    monkeypatch.setattr(hosted_plugins, "_archive_bytes", archive_with_interleaved_render)
    output = hosted_plugins.archive(root, tmp_path / "archive", platform="openai")

    lock = json.loads((output / "openai.zip.lock.json").read_text(encoding="utf-8"))
    assert lock["registered_app_id_sha256"] == hosted_plugins._sha256(
        first_id.encode("utf-8")
    )
    with zipfile.ZipFile(output / "openai.zip") as package:
        app = json.loads(package.read(".app.json"))
    assert app["apps"]["exomem"]["id"] == first_id


def test_openai_archive_blocks_an_interleaved_claude_render(
    tmp_path: Path, monkeypatch
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    hosted_plugins.render(
        root,
        openai_app_id="plugin_asdk_app_releaseinput123",
        platform="openai",
    )
    original_archive = hosted_plugins._archive_bytes

    def archive_with_interleaved_render(package: Path) -> bytes:
        with pytest.raises(ValueError, match="another process"):
            hosted_plugins.render(root, platform="claude")
        return original_archive(package)

    monkeypatch.setattr(hosted_plugins, "_archive_bytes", archive_with_interleaved_render)
    hosted_plugins.archive(root, tmp_path / "archive", platform="openai")


def test_managed_render_blocks_an_interleaved_other_platform_render(
    tmp_path: Path, monkeypatch
) -> None:
    root = copy_hosted_tree(tmp_path / "repo")
    generated = root / "plugins/hosted/generated"

    def openai_files() -> dict[str, bytes]:
        paths = [
            *(path for path in (generated / "openai").rglob("*") if path.is_file()),
            generated / "openai.lock.json",
            generated / "openai.zip",
            generated / "openai.zip.lock.json",
        ]
        return {
            path.relative_to(generated).as_posix(): path.read_bytes() for path in paths
        }

    existing_openai_files = openai_files()
    original_copytree = hosted_plugins.shutil.copytree
    interleaved = False

    def copytree_with_interleaved_render(
        source: Path, destination: Path, *args, **kwargs
    ):
        nonlocal interleaved
        result = original_copytree(source, destination, *args, **kwargs)
        if Path(source).resolve() == generated.resolve() and not interleaved:
            interleaved = True
            with pytest.raises(ValueError, match="another process"):
                hosted_plugins.render(
                    root,
                    openai_app_id="plugin_asdk_app_otherrelease456",
                    platform="openai",
                )
        return result

    monkeypatch.setattr(
        hosted_plugins.shutil, "copytree", copytree_with_interleaved_render
    )
    hosted_plugins.render(root, platform="claude")
    assert openai_files() == existing_openai_files


def test_rendered_packages_are_deterministic_and_remote_only(tmp_path: Path) -> None:
    first = hosted_plugins.render(
        REPO_ROOT,
        tmp_path / "first",
        openai_app_id="plugin_asdk_app_releaseinput123",
        platform="all",
        staging_root=tmp_path,
    )
    second = hosted_plugins.render(
        REPO_ROOT,
        tmp_path / "second",
        openai_app_id="plugin_asdk_app_releaseinput123",
        platform="all",
        staging_root=tmp_path,
    )

    def contents(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    assert contents(first) == contents(second)
    claude_mcp = json.loads((first / "claude/.mcp.json").read_text(encoding="utf-8"))
    openai_app = json.loads((first / "openai/.app.json").read_text(encoding="utf-8"))
    assert claude_mcp["mcpServers"]["exomem"] == {
        "type": "http",
        "url": "https://substratesystems.io/api/exomem/mcp/v1",
    }
    openai_mcp = json.loads((first / "openai/.mcp.json").read_text(encoding="utf-8"))
    assert openai_mcp == {
        "mcp_servers": {"exomem": {"type": "http", "url": "https://substratesystems.io/api/exomem/mcp/v1"}}
    }
    openai_plugin = json.loads(
        (first / "openai/.codex-plugin/plugin.json").read_text(encoding="utf-8")
    )
    assert openai_plugin["skills"] == "./skills/"
    assert openai_plugin["mcpServers"] == "./.mcp.json"
    assert openai_plugin["apps"] == "./.app.json"
    assert openai_app == {
        "apps": {"exomem": {"id": "plugin_asdk_app_releaseinput123", "category": "productivity"}}
    }
    marketplace = json.loads((first / "openai/marketplace.json").read_text(encoding="utf-8"))
    assert marketplace["plugins"][0] == {
        "name": "exomem-hosted",
        "source": {"source": "local", "path": "./plugins/exomem-hosted"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "productivity",
    }
    assert marketplace["interface"] == {"displayName": "Exomem Hosted"}
    assert openai_plugin["interface"]["defaultPrompt"] == ["Use governed long-term memory."]
    hosted_plugins.validate_openai_candidate(first / "openai")
    marketplace["interface"]["defaultPrompt"] = ["unsupported"]
    (first / "openai/marketplace.json").write_text(json.dumps(marketplace), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported fields"):
        hosted_plugins.validate_openai_candidate(first / "openai")
    text_payload = b"\n".join(
        content for name, content in contents(first).items() if not name.endswith(".zip")
    ).decode("utf-8")
    assert "uvx" not in text_payload
