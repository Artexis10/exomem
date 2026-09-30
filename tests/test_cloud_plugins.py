from __future__ import annotations

import hashlib
import json
import struct
import tomllib
import zipfile
from pathlib import Path

from exomem import cloud_plugins, package_skills, workflow_skills
from exomem.public_artifact_privacy import assert_public_artifacts_clean

ROOT = Path(__file__).resolve().parents[1]


def test_openai_submission_icons_are_contained_square_pngs(tmp_path: Path) -> None:
    cloud_plugins.build_packages(ROOT, tmp_path)
    with zipfile.ZipFile(tmp_path / "openai.zip") as archive:
        manifest = json.loads(archive.read("plugin.json"))
        interface = manifest["extensions"]["com.openai"]["interface"]
        for key, minimum in (("logo", 256), ("composerIcon", 48)):
            relative = interface[key].removeprefix("./")
            assert relative == "assets/icon.png"
            data = archive.read(relative)
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            width, height = struct.unpack(">II", data[16:24])
            assert width == height and minimum <= width <= 4096
            assert len(data) <= 5 * 1024 * 1024
            assert data == (ROOT / "plugins/cloud/assets/icon.png").read_bytes()


def test_cloud_packages_share_complete_canonical_skill_bytes(tmp_path: Path) -> None:
    release = cloud_plugins.build_packages(ROOT, tmp_path)

    assert release["version"] == tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
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
    assert json.loads((tmp_path / "openai/mcp.json").read_text())["mcpServers"] == {
        "exomem": {"type": "streamable-http", "url": "https://exomem.substratesystems.io/mcp"}
    }
    archives = [tmp_path / "claude.zip", tmp_path / "openai.zip"]
    assert_public_artifacts_clean(
        archives,
        labels={path: f"plugins/cloud/generated/{path.name}" for path in archives},
    )
