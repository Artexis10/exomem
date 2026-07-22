"""Fail-closed privacy checks for every public authoring artifact class.

All examples in this module are invented canaries.  The gate must never need a
real vault, a copied excerpt, or a corpus-specific token list to prove the build
boundary.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import runpy
import stat
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from exomem import install_skill, package_skills
from exomem.public_artifact_privacy import (
    PUBLIC_ARTIFACT_INVENTORY,
    BinaryProvenance,
    PublicArtifactPrivacyError,
    assert_public_artifacts_clean,
    repository_input_paths,
    scan_artifact,
    scan_repository_inputs,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VAULT_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures"
SYNTHETIC_FIXTURES = (
    REPO_ROOT / "tests" / "privacy_fixtures" / "public_artifact_privacy"
)


def test_inventory_names_every_public_input_and_output_class() -> None:
    assert {item.name for item in PUBLIC_ARTIFACT_INVENTORY} == {
        "package-source",
        "plugin-marketplace",
        "documentation",
        "openspec",
        "tests-fixtures-examples",
        "example-scripts",
        "generated-schemas-docs",
        "build-metadata",
        "root-docs-config-release",
        "agent-config",
        "deployment-examples",
        "sidecars",
        "wheel-sdist",
        "filesystem-installs",
        "skill-plugin-archives",
    }


def test_repository_inventory_enumerates_root_release_and_deploy_inputs() -> None:
    enumerated = set(repository_input_paths(REPO_ROOT))

    assert {
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "QUICKSTART.md",
        ".env.example",
        "env.example",
        "compose.yaml",
        "compose.ml.yaml",
        "compose.cuda.yaml",
        ".dockerignore",
        "release-please-config.json",
        ".release-please-manifest.json",
        "deploy/cloudflare-ha/wrangler.toml.example",
        "infra/terraform/foundation/versions.tf",
    } <= enumerated


def test_committed_privacy_corpus_is_public_but_not_a_vault_fixture() -> None:
    enumerated = set(repository_input_paths(REPO_ROOT))
    relative_root = SYNTHETIC_FIXTURES.relative_to(REPO_ROOT).as_posix()

    assert SYNTHETIC_FIXTURES.is_dir()
    assert not any(
        path.is_file()
        for path in (VAULT_FIXTURE_ROOT / "public_artifact_privacy").rglob("*")
    )
    assert {
        f"{relative_root}/generic-note.md",
        f"{relative_root}/manifest.json",
    } <= enumerated


def test_repository_public_inputs_are_declared_and_clean() -> None:
    report = scan_repository_inputs(REPO_ROOT)

    assert report.scanned_files > 0
    assert report.scanned_text_files > 0
    assert report.findings == ()


def test_sdist_manifest_is_fail_closed_to_package_inputs() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    includes = project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]

    assert set(includes) == {"/src", "/LICENSE", "/README.md", "/pyproject.toml"}
    assert all("*" not in item for item in includes)


def test_repository_gate_does_not_read_ignored_maintainer_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "synthetic-checkout"
    package_file = repository / "src" / "exomem" / "generic.py"
    ignored = repository / "scripts" / "generic" / "leakguard.txt"
    package_file.parent.mkdir(parents=True)
    ignored.parent.mkdir(parents=True)
    package_file.write_text("VALUE = 'generic'\n", encoding="utf-8")
    ignored_canary = "C:" + "\\Users\\" + "SyntheticOperator\\Notebook\n"
    ignored.write_text(ignored_canary, encoding="utf-8")
    (repository / ".gitignore").write_text(
        "scripts/generic/leakguard.txt\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)

    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.resolve() == ignored.resolve():
            pytest.fail("public inventory read an ignored maintainer-only input")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    report = scan_repository_inputs(repository)

    assert report.findings == ()
    assert report.scanned_files == 2


@pytest.mark.parametrize(
    "tracked_relative",
    [
        ".pytest-private/tracked.md",
        "node_modules/example-package/tracked.md",
        "src/__pycache__/tracked.md",
    ],
)
def test_tracked_cache_shaped_inputs_are_never_filtered(
    tmp_path: Path, tracked_relative: str
) -> None:
    repository = tmp_path / "synthetic-checkout"
    repository.mkdir()
    (repository / ".gitignore").write_text(
        ".pytest-*/\nnode_modules/\n__pycache__/\n",
        encoding="utf-8",
    )
    tracked = repository / tracked_relative
    tracked.parent.mkdir(parents=True)
    drive_path = "C:" + "\\" + r"Users\SyntheticOperator\Private-Notebook"
    tracked.write_text(
        f"origin: {drive_path}\n",
        encoding="utf-8",
    )
    ignored_untracked = repository / "node_modules" / "ignored-untracked.md"
    ignored_untracked.parent.mkdir(parents=True, exist_ok=True)
    ignored_untracked.write_text("generic ignored scratch\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", ".gitignore"], cwd=repository, check=True)
    subprocess.run(
        ["git", "add", "--force", "--", tracked_relative],
        cwd=repository,
        check=True,
    )

    enumerated = set(repository_input_paths(repository))
    report = scan_repository_inputs(repository)

    assert tracked_relative in enumerated
    if ignored_untracked != tracked:
        assert "node_modules/ignored-untracked.md" not in enumerated
    assert [(item.rule, item.file, item.line) for item in report.findings] == [
        ("absolute_local_path", tracked_relative, 1)
    ]


def test_repository_text_rejects_drive_root_and_unc_paths_with_redacted_findings(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "synthetic-checkout"
    repository.mkdir()
    drive_path = "D:" + "\\" + r"PrivateVault\notes\secret.md"
    unc_path = "\\" * 2 + r"private-host\share\secret.md"
    tracked = repository / "docs" / "paths.md"
    tracked.parent.mkdir()
    tracked.write_text(
        f"drive: {drive_path}\nunc: {unc_path}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "docs/paths.md"], cwd=repository, check=True)

    report = scan_repository_inputs(repository)
    rendered = "\n".join(str(item) for item in report.findings)

    assert [(item.rule, item.file, item.line) for item in report.findings] == [
        ("absolute_local_path", "docs/paths.md", 1),
        ("absolute_local_path", "docs/paths.md", 2),
    ]
    assert drive_path not in rendered
    assert unc_path not in rendered
    assert "PrivateVault" not in rendered
    assert "private-host" not in rendered
    assert all(set(item.as_dict()) == {"rule", "file", "line"} for item in report.findings)


def test_windows_absolute_path_rule_retains_explicit_placeholder_exemptions(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "placeholders.md"
    slash = "\\"
    artifact.write_text(
        f"vault: C:{slash}path{slash}to{slash}your{slash}vault\n"
        f"user: C:{slash}Users{slash}<name>{slash}vault\n"
        f"example: C:{slash}Users{slash}example{slash}vault\n"
        f"unc-angle: {slash * 2}<server>{slash}<share>{slash}note.md\n"
        f"unc-example: {slash * 2}example{slash}share{slash}note.md\n",
        encoding="utf-8",
    )

    assert scan_artifact(artifact, label="docs/placeholders.md") == ()


def test_repository_text_rejects_unc_share_names_with_spaces_without_echo(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "synthetic-checkout"
    repository.mkdir()
    slash = "\\"
    unc_paths = (
        slash * 2 + "private-host" + slash + "Private Share" + slash + "note.md",
        slash * 2 + "10.0.0.1" + slash + "Team Share" + slash + "note.md",
    )
    tracked = repository / "docs" / "unc-paths.md"
    tracked.parent.mkdir()
    tracked.write_text("\n".join(unc_paths) + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "docs/unc-paths.md"], cwd=repository, check=True)

    report = scan_repository_inputs(repository)
    rendered = "\n".join(str(item) for item in report.findings)

    assert [(item.rule, item.file, item.line) for item in report.findings] == [
        ("absolute_local_path", "docs/unc-paths.md", 1),
        ("absolute_local_path", "docs/unc-paths.md", 2),
    ]
    assert all(path not in rendered for path in unc_paths)
    assert "private-host" not in rendered
    assert "Private Share" not in rendered
    assert "10.0.0.1" not in rendered
    assert "Team Share" not in rendered


def test_new_unclassified_root_format_fails_closed(tmp_path: Path) -> None:
    repository = tmp_path / "synthetic-checkout"
    repository.mkdir()
    (repository / "novel.public-format").write_text("generic\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)

    report = scan_repository_inputs(repository)

    assert [(item.rule, item.file, item.line) for item in report.findings] == [
        ("format_provenance_missing", "novel.public-format", 0)
    ]


def test_new_or_binary_format_fails_without_explicit_provenance(tmp_path: Path) -> None:
    opaque = tmp_path / "new-format.bin"
    opaque.write_bytes(b"\x00\x01\x02")

    findings = scan_artifact(opaque, label="generated/new-format.bin")

    assert [(item.rule, item.file, item.line) for item in findings] == [
        ("format_provenance_missing", "generated/new-format.bin", 0)
    ]


def test_explicit_binary_provenance_is_narrow_and_auditable(tmp_path: Path) -> None:
    opaque = tmp_path / "brand-icon.bin"
    opaque.write_bytes(b"\x00\x01\x02")

    findings = scan_artifact(
        opaque,
        label="generated/brand-icon.bin",
        binary_provenance=(
            BinaryProvenance("generated/brand-icon.bin", "repository-authored test icon"),
        ),
    )

    assert findings == ()


def test_archive_members_are_scanned_by_name_and_supported_text(tmp_path: Path) -> None:
    archive_path = tmp_path / "generic-skill.zip"
    canary = "C:" + "\\Users\\" + "SyntheticOperator\\Confidential-Notes"
    unsafe_member = "C:" + "/Users/" + "SyntheticOperator/secret.md"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("SKILL.md", f"# Generic skill\n\nSource: {canary}\n")
        archive.writestr(unsafe_member, "safe body\n")
        archive.writestr("payload.bin", b"\x00\x01")

    findings = scan_artifact(archive_path, label="dist/generic-skill.zip")
    rendered = "\n".join(str(item) for item in findings)

    assert {item.rule for item in findings} == {
        "absolute_local_path",
        "archive_member_path",
        "format_provenance_missing",
    }
    assert all(set(item.as_dict()) == {"rule", "file", "line"} for item in findings)
    assert "SyntheticOperator" not in rendered
    assert "Confidential-Notes" not in rendered
    assert canary not in rendered


def test_nested_member_name_content_is_scanned_without_rendering_the_name(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "generic-skill.zip"
    private_member = (
        "prefix/C:" + "/Users/" + "SyntheticOperator/Private-Notebook.md"
    )
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(private_member, "generic body\n")

    findings = scan_artifact(archive_path, label="dist/generic-skill.zip")
    rendered = "\n".join(str(item) for item in findings)

    assert {item.rule for item in findings} == {"absolute_local_path"}
    assert "SyntheticOperator" not in rendered
    assert "Private-Notebook" not in rendered
    assert private_member not in rendered
    assert all("!member-" in item.file for item in findings)


def test_content_finding_never_renders_an_otherwise_safe_private_member_name(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "generic-skill.zip"
    private_member = "InventedPrivateClient/note.md"
    canary = "C:" + "\\Users\\" + "SyntheticOperator\\Notebook"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(private_member, f"origin: {canary}\n")

    findings = scan_artifact(archive_path, label="dist/generic-skill.zip")
    rendered = "\n".join(str(item) for item in findings)

    assert {item.rule for item in findings} == {"absolute_local_path"}
    assert "InventedPrivateClient" not in rendered
    assert "SyntheticOperator" not in rendered
    assert all("!member-" in item.file for item in findings)


def test_nested_zip_and_tar_member_names_are_scanned_and_redacted(tmp_path: Path) -> None:
    nested_canary = "C:" + "/Users/" + "SyntheticOperator/Notebook.md"

    inner_zip = io.BytesIO()
    with zipfile.ZipFile(inner_zip, "w") as archive:
        archive.writestr(nested_canary, "generic zip body\n")

    inner_tar = io.BytesIO()
    with tarfile.open(fileobj=inner_tar, mode="w:gz") as archive:
        body = b"generic tar body\n"
        member = tarfile.TarInfo(f"prefix/{nested_canary}")
        member.size = len(body)
        archive.addfile(member, io.BytesIO(body))

    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("nested/inner.zip", inner_zip.getvalue())
        archive.writestr("nested/inner.tar.gz", inner_tar.getvalue())

    findings = scan_artifact(outer, label="dist/outer.zip")
    rendered = "\n".join(str(item) for item in findings)

    assert [item.rule for item in findings].count("absolute_local_path") == 2
    assert "SyntheticOperator" not in rendered
    assert nested_canary not in rendered
    assert all("!member-" in item.file for item in findings)


def test_nested_archive_text_rejects_drive_root_and_unc_paths_without_echo(
    tmp_path: Path,
) -> None:
    drive_path = "E:" + "/PrivateVault/notes/secret.md"
    unc_path = "\\" * 2 + r"private-host\share\secret.md"
    inner_zip = io.BytesIO()
    with zipfile.ZipFile(inner_zip, "w") as archive:
        archive.writestr(
            "notes.md",
            f"drive: {drive_path}\nunc: {unc_path}\n",
        )

    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("nested/inner.zip", inner_zip.getvalue())

    findings = scan_artifact(outer, label="dist/outer.zip")
    rendered = "\n".join(str(item) for item in findings)

    assert [(item.rule, item.line) for item in findings] == [
        ("absolute_local_path", 1),
        ("absolute_local_path", 2),
    ]
    assert all(set(item.as_dict()) == {"rule", "file", "line"} for item in findings)
    assert all("!member-" in item.file for item in findings)
    assert drive_path not in rendered
    assert unc_path not in rendered
    assert "PrivateVault" not in rendered
    assert "private-host" not in rendered


def test_nested_archive_text_rejects_unc_share_names_with_spaces_without_echo(
    tmp_path: Path,
) -> None:
    slash = "\\"
    unc_paths = (
        slash * 2 + "private-host" + slash + "Private Share" + slash + "note.md",
        slash * 2 + "10.0.0.1" + slash + "Team Share" + slash + "note.md",
    )
    inner_zip = io.BytesIO()
    with zipfile.ZipFile(inner_zip, "w") as archive:
        archive.writestr("notes.md", "\n".join(unc_paths) + "\n")

    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("nested/inner.zip", inner_zip.getvalue())

    findings = scan_artifact(outer, label="dist/outer.zip")
    rendered = "\n".join(str(item) for item in findings)

    assert [(item.rule, item.line) for item in findings] == [
        ("absolute_local_path", 1),
        ("absolute_local_path", 2),
    ]
    assert all(path not in rendered for path in unc_paths)
    assert "private-host" not in rendered
    assert "Private Share" not in rendered
    assert "10.0.0.1" not in rendered
    assert "Team Share" not in rendered
    assert all("!member-" in item.file for item in findings)


@pytest.mark.parametrize(
    "entry_mode",
    [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK],
)
def test_zip_rejects_every_nonregular_entry_type(
    tmp_path: Path, entry_mode: int
) -> None:
    archive_path = tmp_path / "special.zip"
    special = zipfile.ZipInfo("private-special-entry")
    special.create_system = 3
    special.external_attr = (entry_mode | 0o600) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(special, "ignored target")

    findings = scan_artifact(archive_path, label="dist/special.zip")
    rendered = "\n".join(str(item) for item in findings)

    assert {item.rule for item in findings} == {"archive_member_type"}
    assert "private-special-entry" not in rendered
    assert all("!member-" in item.file for item in findings)


@pytest.mark.parametrize("error_type", [zipfile.BadZipFile, ValueError])
def test_archive_read_error_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    archive_path = tmp_path / "broken.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("InventedPrivateClient/note.md", "generic\n")

    def fail_read(*_args: object, **_kwargs: object) -> bytes:
        raise error_type("InventedPrivateClient/note.md")

    monkeypatch.setattr(zipfile.ZipFile, "read", fail_read)
    findings = scan_artifact(archive_path, label="dist/broken.zip")
    rendered = "\n".join(str(item) for item in findings)

    assert [(item.rule, item.file, item.line) for item in findings] == [
        ("invalid_archive", "dist/broken.zip", 0)
    ]
    assert "InventedPrivateClient" not in rendered


def test_filesystem_nonregular_is_rejected_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    special = tmp_path / "special.md"
    fake_stat = os.stat_result((stat.S_IFIFO | 0o600,) + (0,) * 9)
    monkeypatch.setattr(Path, "lstat", lambda _path: fake_stat)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: pytest.fail("nonregular entry must not be opened"),
    )

    findings = scan_artifact(special, label="dist/special.md")

    assert [(item.rule, item.file, item.line) for item in findings] == [
        ("filesystem_entry_type", "dist/special.md", 0)
    ]


def test_archive_links_cannot_follow_an_external_input(tmp_path: Path) -> None:
    archive_path = tmp_path / "linked-skill.zip"
    linked = zipfile.ZipInfo("linked-note.md")
    linked.create_system = 3
    linked.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(linked, "../../outside-repository.md")

    findings = scan_artifact(archive_path, label="dist/linked-skill.zip")

    assert [item.rule for item in findings] == ["archive_member_type"]
    assert findings[0].file.startswith("dist/linked-skill.zip!member-")
    assert "linked-note.md" not in findings[0].file


def test_invalid_utf8_is_not_silently_treated_as_text(tmp_path: Path) -> None:
    invalid = tmp_path / "broken.md"
    invalid.write_bytes(b"valid prefix\n\xff\xfe")

    findings = scan_artifact(invalid, label="docs/broken.md")

    assert [(item.rule, item.file, item.line) for item in findings] == [
        ("invalid_utf8", "docs/broken.md", 0)
    ]


def test_assertion_error_redacts_matched_source_content(tmp_path: Path) -> None:
    canary = "C:" + "\\Users\\" + "InventedMaintainer\\Private-Notebook"
    leaked = tmp_path / "leaked.md"
    leaked.write_text(f"origin: {canary}\n", encoding="utf-8")

    with pytest.raises(PublicArtifactPrivacyError) as exc_info:
        assert_public_artifacts_clean((leaked,), labels={leaked: "docs/leaked.md"})

    message = str(exc_info.value)
    assert "absolute_local_path" in message
    assert "docs/leaked.md:1" in message
    assert "InventedMaintainer" not in message
    assert "Private-Notebook" not in message
    assert canary not in message


def test_committed_privacy_corpus_is_generated_from_generic_constants() -> None:
    namespace = runpy.run_path(str(REPO_ROOT / "scripts" / "generate-public-privacy-fixtures.py"))
    expected = namespace["render_corpus"]()
    actual = {
        str(path.relative_to(SYNTHETIC_FIXTURES)).replace("\\", "/"): path.read_text(
            encoding="utf-8"
        )
        for path in sorted(SYNTHETIC_FIXTURES.rglob("*"))
        if path.is_file()
    }

    assert actual == expected
    manifest = json.loads(actual["manifest.json"])
    assert manifest["provenance"] == "committed synthetic generator"
    assert manifest["live_vault_input"] is False


def test_generic_public_builds_never_read_configured_live_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_vault = tmp_path / "live-vault-canary"
    registry = private_vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text("projects:\n  forbidden-canary: {}\n", encoding="utf-8")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(private_vault))

    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    original_open = Path.open
    private_root = private_vault.resolve()

    def reject_private_path(path: Path) -> None:
        resolved = path.resolve()
        if resolved == private_root or private_root in resolved.parents:
            pytest.fail("a generic public build attempted to read the live-vault canary")

    def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
        reject_private_path(path)
        return original_read_text(path, *args, **kwargs)

    def guarded_read_bytes(path: Path) -> bytes:
        reject_private_path(path)
        return original_read_bytes(path)

    def guarded_open(path: Path, *args: object, **kwargs: object):
        reject_private_path(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "open", guarded_open)

    archives = tmp_path / "generic-archives"
    plugin = tmp_path / "generic-plugin"
    installed = tmp_path / "generic-install" / "exomem"
    package_skills.package_skills(archives)
    package_skills.sync_plugin(plugin)
    install_skill.install_skill(installed)

    generator_path = REPO_ROOT / "scripts" / "generate-capabilities.py"
    spec = importlib.util.spec_from_file_location("generic_capability_generator", generator_path)
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    rendered_docs = generator.build_capabilities_markdown()
    schema_generator = runpy.run_path(str(REPO_ROOT / "scripts" / "dump-tool-schemas.py"))

    assert "forbidden-canary" not in rendered_docs
    assert Path(schema_generator["FIXTURE_VAULT"]).resolve() == (
        REPO_ROOT / "tests" / "fixtures"
    ).resolve()
    for artifact in archives.glob("*.zip"):
        with zipfile.ZipFile(artifact) as archive:
            assert all(b"forbidden-canary" not in archive.read(name) for name in archive.namelist())
    assert all(
        "forbidden-canary" not in path.read_text(encoding="utf-8")
        for path in plugin.rglob("*")
        if path.is_file()
    )


@pytest.mark.parametrize(
    "public_root",
    [
        "src",
        "plugins",
        ".claude-plugin",
        "docs",
        "openspec",
        "tests/fixtures",
        "scripts",
        "dist",
        "build",
    ],
)
def test_personalized_output_cannot_feed_public_release_roots(
    tmp_path: Path, public_root: str
) -> None:
    vault = tmp_path / "synthetic-vault"
    registry = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text("projects:\n  synthetic-private: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="personalized output"):
        package_skills.package_skills(REPO_ROOT / public_root / "private-build", vault=vault)


def test_explicit_personalized_output_stays_outside_public_release_roots(tmp_path: Path) -> None:
    vault = tmp_path / "synthetic-vault"
    registry = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text("projects:\n  synthetic-private: {}\n", encoding="utf-8")
    output = vault / "Knowledge Base" / "_Schema" / "private-skills"

    report = package_skills.package_skills(output, vault=vault)

    assert report["personalized"] is True
    assert Path(report["out_dir"]).resolve() == output.resolve()
    try:
        relative = output.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        relative = None
    if relative is not None:
        assert relative.parts[0].startswith(".pytest-") or relative.parts[0] == (
            ".exomem-private"
        )
    with zipfile.ZipFile(output / "exomem.zip") as archive:
        assert b"synthetic-private" in archive.read("project-keys.yaml")
        assert package_skills.PRIVATE_OUTPUT_MARKER in archive.namelist()
    findings = scan_artifact(output / "exomem.zip", label="dist/copied-personalized.zip")
    assert [item.rule for item in findings] == [
        "personalized_artifact_in_public_build"
    ]
    assert findings[0].file.startswith("dist/copied-personalized.zip!member-")
    assert package_skills.PRIVATE_OUTPUT_MARKER not in findings[0].file


def test_personalized_output_must_be_inside_explicit_vault(tmp_path: Path) -> None:
    vault = tmp_path / "synthetic-vault"
    vault.mkdir()

    with pytest.raises(ValueError, match="inside the explicitly supplied vault"):
        package_skills.ensure_personalized_output(
            tmp_path / "unrelated-private-output",
            vault=vault,
        )


def test_installed_package_layout_still_rejects_checkout_release_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "synthetic-vault"
    vault.mkdir()
    simulated_install = tmp_path / "site-packages" / "exomem" / "package_skills.py"
    monkeypatch.setattr(package_skills, "__file__", str(simulated_install))

    with pytest.raises(ValueError, match="public/release"):
        package_skills.ensure_personalized_output(
            REPO_ROOT / "dist" / "personalized",
            vault=vault,
        )


def test_rebuild_wrapper_ignores_ambient_vault_without_explicit_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "synthetic-vault"
    registry = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text("projects:\n  must-not-ship: {}\n", encoding="utf-8")
    output = tmp_path / "generic-schema.zip"
    env = {**os.environ, "EXOMEM_VAULT_PATH": str(vault)}

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "rebuild-schema-zip.py"),
            "--out",
            str(output),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    with zipfile.ZipFile(output) as archive:
        assert package_skills.PRIVATE_OUTPUT_MARKER not in archive.namelist()
        assert b"must-not-ship" not in archive.read("project-keys.yaml")


def test_default_package_does_not_implicitly_use_vault_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from exomem.__main__ import main

    vault = tmp_path / "synthetic-vault"
    registry = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text("projects:\n  must-not-ship: {}\n", encoding="utf-8")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    output = tmp_path / "generic"

    assert main(["package-skills", "--out", str(output)]) == 0
    capsys.readouterr()
    with zipfile.ZipFile(output / "exomem.zip") as archive:
        assert b"must-not-ship" not in archive.read("project-keys.yaml")
