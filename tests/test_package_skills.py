"""package-skills - build uploadable archives for clients with no filesystem.

claude.ai and ChatGPT offer no install API, so a human uploads an archive. The
value here is that all ten skills get built from the one scaffold source, instead
of one being hand-zipped and the other nine forgotten.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from exomem import package_skills as package_module
from exomem import semantic_authoring, workflow_skills
from exomem.public_artifact_privacy import assert_public_artifacts_clean

REPO_ROOT = Path(__file__).resolve().parents[1]


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _assert_contract(text: str) -> None:
    normalized = _normalized(text)
    contract = semantic_authoring.AUTHORING_CONTRACT
    concise = semantic_authoring.render_concise()
    assert concise in normalized
    assert f"exomem-semantic-authoring:v{contract.version}" in normalized
    assert contract.content_digest in normalized


def test_packages_every_skill_not_just_the_core_one(tmp_path: Path) -> None:
    report = package_module.package_skills(tmp_path)

    expected = {"exomem"} | {str(s["name"]) for s in workflow_skills.list_skills()}
    produced = {a["name"] for a in report["archives"]}
    assert produced == expected
    assert report["count"] == len(expected)
    for archive in report["archives"]:
        assert Path(archive["path"]).is_file()
    public_archives = [Path(archive["path"]) for archive in report["archives"]]
    assert_public_artifacts_clean(
        public_archives,
        labels={path: f"skill-archives/{path.name}" for path in public_archives},
    )


def test_skill_md_sits_at_the_archive_root(tmp_path: Path) -> None:
    """Web uploaders expect SKILL.md at the root, not nested under a folder."""
    package_module.package_skills(tmp_path)

    for name in ("exomem", "exomem-capture"):
        with zipfile.ZipFile(tmp_path / f"{name}.zip") as archive:
            assert "SKILL.md" in archive.namelist()


def test_core_archive_carries_its_references(tmp_path: Path) -> None:
    """The web clients cannot reach the repo, so references must travel with it."""
    package_module.package_skills(tmp_path)

    with zipfile.ZipFile(tmp_path / "exomem.zip") as archive:
        names = archive.namelist()
    assert any(n.startswith("references/") for n in names)
    assert "project-keys.yaml" in names


def test_generic_build_ships_the_scaffold_keys(tmp_path: Path) -> None:
    """Without a vault the archive must stay shareable - no personal registry."""
    package_module.package_skills(tmp_path)

    with zipfile.ZipFile(tmp_path / "exomem.zip") as archive:
        packaged = archive.read("project-keys.yaml").decode("utf-8")
    scaffold = (
        Path(package_module.__file__).parent / "_scaffold" / "_Schema" / "project-keys.yaml"
    ).read_text(encoding="utf-8")
    assert packaged == scaffold


def test_vault_overlay_replaces_only_the_project_key_registry(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    schema = vault / "Knowledge Base" / "_Schema"
    schema.mkdir(parents=True)
    (schema / "project-keys.yaml").write_text("projects:\n  real-one: {}\n", encoding="utf-8")

    output = vault / "private-skills"
    package_module.package_skills(output, vault=vault)

    with zipfile.ZipFile(output / "exomem.zip") as archive:
        assert "real-one" in archive.read("project-keys.yaml").decode("utf-8")
        assert package_module.PRIVATE_OUTPUT_MARKER in archive.namelist()
        # SKILL.md still comes from the scaffold, never from the vault.
        assert archive.read("SKILL.md").decode("utf-8").startswith("---")


def test_rebuild_overwrites_a_previous_archive(tmp_path: Path) -> None:
    """Re-running must replace, not append into, an existing zip."""
    package_module.package_skills(tmp_path)
    first = (tmp_path / "exomem.zip").stat().st_size

    package_module.package_skills(tmp_path)

    assert (tmp_path / "exomem.zip").stat().st_size == first
    with zipfile.ZipFile(tmp_path / "exomem.zip") as archive:
        assert archive.namelist().count("SKILL.md") == 1


def test_missing_scaffold_is_reported_not_silently_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(package_module, "_SKILL_SRC", tmp_path / "nope")

    with pytest.raises(FileNotFoundError, match="bundled skill missing"):
        package_module.package_skills(tmp_path / "out")


def test_every_authoring_archive_is_standalone(tmp_path: Path) -> None:
    report = package_module.package_skills(tmp_path)
    authoring = {
        str(skill["name"])
        for skill in workflow_skills.list_skills()
        if skill.get("standalone_authoring") is True
    }
    assert authoring

    for item in report["archives"]:
        if item["name"] not in authoring:
            continue
        with zipfile.ZipFile(item["path"]) as archive:
            _assert_contract(archive.read("SKILL.md").decode("utf-8"))


def test_packaging_rejects_authoring_workflow_that_only_references_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authoring = next(
        str(skill["name"])
        for skill in workflow_skills.list_skills()
        if skill.get("standalone_authoring") is True
    )
    fake = tmp_path / authoring
    fake.mkdir()
    (fake / "SKILL.md").write_text(
        f"---\nname: {authoring}\n---\n\nSee the exomem core skill.\n",
        encoding="utf-8",
    )
    original = workflow_skills.source_dir
    monkeypatch.setattr(
        workflow_skills,
        "source_dir",
        lambda name: fake if name == authoring else original(name),
    )

    with pytest.raises(ValueError, match=f"{authoring}.*semantic authoring contract"):
        package_module.package_skills(tmp_path / "out")


@pytest.mark.timeout(180)
def test_builds_and_unpacks_every_public_contract_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = semantic_authoring.AUTHORING_CONTRACT
    concise = semantic_authoring.render_concise()
    skills = [str(skill["name"]) for skill in workflow_skills.list_skills()]

    # Generic uploadable archives.
    archive_dir = tmp_path / "skills"
    package_module.package_skills(archive_dir)
    with zipfile.ZipFile(archive_dir / "exomem.zip") as archive:
        _assert_contract(archive.read("SKILL.md").decode("utf-8"))
        assert "references/page-types.md" in archive.namelist()
        assert "project-keys.yaml" in archive.namelist()
    for name in skills:
        with zipfile.ZipFile(archive_dir / f"{name}.zip") as archive:
            _assert_contract(archive.read("SKILL.md").decode("utf-8"))

    # Generated plugin output.
    plugin = tmp_path / "plugin"
    package_module.sync_plugin(plugin)
    _assert_contract((plugin / "skills" / "exomem" / "SKILL.md").read_text("utf-8"))
    assert (plugin / "skills" / "exomem" / "references" / "page-types.md").is_file()
    for name in skills:
        _assert_contract((plugin / "skills" / name / "SKILL.md").read_text("utf-8"))
    plugin_files = [path for path in plugin.rglob("*") if path.is_file()]
    assert_public_artifacts_clean(
        plugin_files,
        labels={
            path: f"plugin/{path.relative_to(plugin).as_posix()}" for path in plugin_files
        },
    )

    # Wheel and sdist include the generic scaffold and runtime contract source.
    build_source = tmp_path / "build-source"
    shutil.copytree(REPO_ROOT / "src", build_source / "src")
    sample_kb = build_source / "src" / "exomem" / "_sample_vault" / "Knowledge Base"
    local_runtime_state = (
        sample_kb / ".refs.sqlite",
        sample_kb / ".embeddings.sqlite-wal",
    )
    for path in local_runtime_state:
        path.write_bytes(b"synthetic ignored local runtime state")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO_ROOT / name, build_source / name)
    dist = tmp_path / "dist"
    env = {"UV_CACHE_DIR": str(tmp_path / "uv-cache")}
    completed = subprocess.run(
        ["uv", "build", "--out-dir", str(dist)],
        cwd=build_source,
        env={**os.environ, **env},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    wheel = next(dist.glob("*.whl"))
    assert_public_artifacts_clean((wheel,), labels={wheel: f"wheel/{wheel.name}"})
    with zipfile.ZipFile(wheel) as built:
        core_path = "exomem/_scaffold/_Schema/SKILL.md"
        assert not {
            "exomem/_sample_vault/Knowledge Base/.refs.sqlite",
            "exomem/_sample_vault/Knowledge Base/.embeddings.sqlite-wal",
        } & set(built.namelist())
        _assert_contract(built.read(core_path).decode("utf-8"))
        assert "exomem/_scaffold/_Schema/references/page-types.md" in built.namelist()
        assert "exomem/semantic_authoring.py" in built.namelist()
        for name in skills:
            path = f"exomem/_scaffold/_Schema/workflow-skills/{name}/SKILL.md"
            _assert_contract(built.read(path).decode("utf-8"))

    sdist = next(dist.glob("*.tar.gz"))
    assert_public_artifacts_clean((sdist,), labels={sdist: f"sdist/{sdist.name}"})
    with tarfile.open(sdist, "r:gz") as built:
        names = built.getnames()
        prefix = names[0].split("/", 1)[0]
        assert not {
            f"{prefix}/src/exomem/_sample_vault/Knowledge Base/.refs.sqlite",
            f"{prefix}/src/exomem/_sample_vault/Knowledge Base/.embeddings.sqlite-wal",
        } & set(names)
        core_path = f"{prefix}/src/exomem/_scaffold/_Schema/SKILL.md"
        core = built.extractfile(core_path)
        assert core is not None
        _assert_contract(core.read().decode("utf-8"))
        assert f"{prefix}/src/exomem/semantic_authoring.py" in names
        assert (
            f"{prefix}/src/exomem/_scaffold/_Schema/references/page-types.md" in names
        )
        for name in skills:
            path = f"{prefix}/src/exomem/_scaffold/_Schema/workflow-skills/{name}/SKILL.md"
            member = built.extractfile(path)
            assert member is not None
            _assert_contract(member.read().decode("utf-8"))

    assert f"v{contract.version}" in concise
    assert contract.content_digest in concise
