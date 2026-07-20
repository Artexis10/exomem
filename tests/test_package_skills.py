"""package-skills - build uploadable archives for clients with no filesystem.

claude.ai and ChatGPT offer no install API, so a human uploads an archive. The
value here is that all ten skills get built from the one scaffold source, instead
of one being hand-zipped and the other nine forgotten.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from exomem import package_skills as package_module
from exomem import workflow_skills


def test_packages_every_skill_not_just_the_core_one(tmp_path: Path) -> None:
    report = package_module.package_skills(tmp_path)

    expected = {"exomem"} | {str(s["name"]) for s in workflow_skills.list_skills()}
    produced = {a["name"] for a in report["archives"]}
    assert produced == expected
    assert report["count"] == len(expected)
    for archive in report["archives"]:
        assert Path(archive["path"]).is_file()


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

    package_module.package_skills(tmp_path / "out", vault=vault)

    with zipfile.ZipFile(tmp_path / "out" / "exomem.zip") as archive:
        assert "real-one" in archive.read("project-keys.yaml").decode("utf-8")
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
