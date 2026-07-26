from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from exomem import hosted_plugins


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_promotion_rejects_discovery_only_or_mocked_evidence() -> None:
    with pytest.raises(ValueError, match="content-bearing"):
        hosted_plugins.promote(REPO_ROOT, "claude", {"mocked": True})


def test_pending_records_are_not_distributed() -> None:
    distribution = hosted_plugins.distribution_manifest(REPO_ROOT)

    assert distribution == {"live_platforms": [], "cross_client_ready": False}


def test_promotion_never_accepts_a_trust_key_from_evidence() -> None:
    with pytest.raises(ValueError, match="operator-trusted signing key"):
        hosted_plugins.promote(
            REPO_ROOT,
            "claude",
            {"operator_key_id": "evidence-key", "operator_signature": "0" * 64},
        )


def test_public_gate_rejects_private_tokens_in_source_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted = tmp_path / "plugins" / "hosted"
    (hosted / "assets").mkdir(parents=True)
    (hosted / "definition.json").write_text("{}", encoding="utf-8")
    (hosted / "behavior-fixtures-v1.json").write_text("{}", encoding="utf-8")
    (hosted / "assets" / "icon.svg").write_text("api_secret=private", encoding="utf-8")
    monkeypatch.setattr(hosted_plugins, "PLUGIN_ROOT", Path("plugins/hosted"))
    monkeypatch.setattr(hosted_plugins, "_skill_paths", lambda root: ())

    with pytest.raises(ValueError, match="unsafe"):
        hosted_plugins.validate_hosted_public_inputs(tmp_path)


def test_public_gate_rejects_private_tokens_in_archive_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted = tmp_path / "plugins" / "hosted"
    (hosted / "generated").mkdir(parents=True)
    (hosted / "definition.json").write_text("{}", encoding="utf-8")
    (hosted / "behavior-fixtures-v1.json").write_text("{}", encoding="utf-8")
    with zipfile.ZipFile(hosted / "generated" / "claude.zip", "w") as archive:
        archive.writestr("skills/exomem/SKILL.md", "api_secret=private")
    monkeypatch.setattr(hosted_plugins, "PLUGIN_ROOT", Path("plugins/hosted"))
    monkeypatch.setattr(hosted_plugins, "_skill_paths", lambda root: ())

    with pytest.raises(ValueError, match="unsafe"):
        hosted_plugins.validate_hosted_public_inputs(tmp_path)
