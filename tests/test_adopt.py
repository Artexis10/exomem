"""Existing-vault adoption workflow."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from importlib import resources
from pathlib import Path

import pytest

from exomem import adopt as adopt_module
from exomem import knowledge_packs
from exomem.__main__ import main


def _snapshot(root: Path, *, exclude_kb: bool = False) -> dict[str, tuple[int, float]]:
    out: dict[str, tuple[int, float]] = {}
    kb = root / "Knowledge Base"
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if exclude_kb and p.is_relative_to(kb):
            continue
        out[p.relative_to(root).as_posix()] = (p.stat().st_size, p.stat().st_mtime)
    return out


def _legacy_vault(root: Path, *, kb: bool = False) -> Path:
    vault = root / "legacy-vault"
    (vault / "Warranty Case").mkdir(parents=True)
    (vault / "Warranty Case" / "laptop-receipt.md").write_text("# Laptop receipt\n\nreceipt\n", encoding="utf-8")
    (vault / "Creative Assets").mkdir()
    (vault / "Creative Assets" / "shoot-reference.md").write_text("photo ideas\n", encoding="utf-8")
    (vault / "Repos").mkdir()
    (vault / "Repos" / "api-incident.md").write_text("deploy failed\n", encoding="utf-8")
    if kb:
        kb_root = vault / "Knowledge Base"
        (kb_root / "Notes").mkdir(parents=True)
        (kb_root / "Sources").mkdir(parents=True)
        (kb_root / "Sources" / "index.md").write_text(
            "# Sources - Index\n\n## By type\n\n## Recent captures\n\n",
            encoding="utf-8",
        )
        (kb_root / "index.md").write_text(
            "# Knowledge Base\n\n## Counts\n\n- Sources: 0\n\n## Recent activity\n\n",
            encoding="utf-8",
        )
        (kb_root / "log.md").write_text("# Log\n\n---\n", encoding="utf-8")
    return vault


def test_adopt_scan_only_is_read_only_before_init(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    before = _snapshot(vault)

    report = adopt_module.adopt(vault)

    assert _snapshot(vault) == before
    assert report["mode"] == "scan-only"
    assert report["governance"]["kb_present"] is False
    assert report["summary"]["kb"] == {"present": False}
    assert {a["action"] for a in report["next_actions"]} == {"scan-only", "initialize-kb"}


def test_adopt_suggests_builtin_packs_from_structure(tmp_path: Path) -> None:
    report = adopt_module.adopt(_legacy_vault(tmp_path, kb=True))
    by_id = {p["id"]: p for p in report["pack_suggestions"]}

    assert {"creative", "legal-warranty", "technical"} <= set(by_id)
    assert by_id["legal-warranty"]["score"] >= 3
    assert by_id["technical"]["score"] >= 3
    assert "creative" in by_id
    assert {p["id"] for p in report["available_packs"]} >= {
        "legal-warranty",
        "creative",
        "technical",
        "health-athletic",
        "business",
        "personal-records",
    }
    assert "required_fields" in report["pack_schema"]
    assert "purpose" in report["pack_schema"]["required_fields"]
    assert report["pack_schema"]["selection_manifest"] == "Knowledge Base/_Packs/selected-packs.json"
    assert by_id["technical"]["beginner_description"]
    assert by_id["legal-warranty"]["suggested_workflows"][0]["route"]
    assert report["governance"]["kb_present"] is True
    assert {a["action"] for a in report["next_actions"]} >= {"save-manifest", "copy-as-sources"}


def test_adopt_save_manifest_writes_only_under_kb(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    before_legacy = _snapshot(vault, exclude_kb=True)

    report = adopt_module.adopt(
        vault,
        mode="save-manifest",
        today=dt.date(2026, 7, 7),
    )

    assert _snapshot(vault, exclude_kb=True) == before_legacy
    manifest = report["manifest"]
    assert manifest["path"].startswith("Knowledge Base/_Adoption/")
    manifest_path = vault / manifest["path"]
    assert manifest_path.exists()
    text = manifest_path.read_text(encoding="utf-8")
    assert "# Adoption Manifest" in text
    assert "Originals stay where they are" in text


def test_adopt_copy_as_sources_preserves_original_and_records_provenance(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    original = vault / "Warranty Case" / "laptop-receipt.md"
    before = original.read_bytes()
    expected_hash = hashlib.sha256(before).hexdigest()

    report = adopt_module.adopt(
        vault,
        mode="copy-as-sources",
        selected_paths=["Warranty Case/laptop-receipt.md"],
        today=dt.date(2026, 7, 7),
    )

    assert original.read_bytes() == before
    copied = report["copy"]["copied_sources"]
    assert len(copied) == 1
    assert copied[0]["original_path"] == "Warranty Case/laptop-receipt.md"
    assert copied[0]["original_sha256"] == expected_hash
    source_path = vault / copied[0]["source_path"]
    assert source_path.exists()
    assert source_path.as_posix().endswith("Knowledge Base/Sources/Imported/2026-07-07-laptop-receipt.md")
    source_text = source_path.read_text(encoding="utf-8")
    assert "imported_from: Warranty Case/laptop-receipt.md" in source_text
    assert f"original_sha256: {expected_hash}" in source_text
    assert "# Laptop receipt" in source_text


def test_adopt_copy_as_sources_disambiguates_same_basename_batch(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    for folder, body in (("Mercor A", "alpha answer"), ("Mercor B", "beta answer")):
        target = vault / folder / "Task1.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# Task1\n\n{body}\n", encoding="utf-8")

    report = adopt_module.adopt(
        vault,
        mode="copy-as-sources",
        selected_paths=["Mercor A/Task1.md", "Mercor B/Task1.md"],
        today=dt.date(2026, 7, 7),
    )

    copied = report["copy"]["copied_sources"]
    source_paths = [item["source_path"] for item in copied]
    assert len(copied) == 2
    assert len(set(source_paths)) == 2
    assert source_paths == [
        "Knowledge Base/Sources/Imported/2026-07-07-task1.md",
        "Knowledge Base/Sources/Imported/2026-07-07-task1-2.md",
    ]
    source_texts = {
        item["original_path"]: (vault / item["source_path"]).read_text(encoding="utf-8")
        for item in copied
    }
    assert "alpha answer" in source_texts["Mercor A/Task1.md"]
    assert "beta answer" in source_texts["Mercor B/Task1.md"]



def test_adopt_compile_selected_copies_and_returns_reviewable_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    original = vault / "Warranty Case" / "laptop-receipt.md"
    before_legacy = _snapshot(vault, exclude_kb=True)

    def fake_propose(root: Path, *, sources: list[str], suggested_title: str | None = None) -> dict:
        assert root == vault
        assert sources == ["Knowledge Base/Sources/Imported/2026-07-07-laptop-receipt"]
        return {
            "suggested_note_type": "insight",
            "suggested_title": "Laptop receipt",
            "suggested_sources": list(sources),
            "suggested_connections": [],
            "outline_markdown": "# Laptop receipt\n\n## Claim\n",
            "warnings": [],
        }

    monkeypatch.setattr(adopt_module.compile_proposal_module, "propose_compilation", fake_propose)

    report = adopt_module.adopt(
        vault,
        mode="compile-selected",
        selected_paths=["Warranty Case/laptop-receipt.md"],
        today=dt.date(2026, 7, 7),
    )

    assert original.read_text(encoding="utf-8") == "# Laptop receipt\n\nreceipt\n"
    assert _snapshot(vault, exclude_kb=True) == before_legacy
    assert list((vault / "Knowledge Base" / "Notes").rglob("*.md")) == []

    plan = report["compile_plan"]
    assert plan["status"] == "ready"
    assert plan["proposal"]["suggested_sources"] == [
        "Knowledge Base/Sources/Imported/2026-07-07-laptop-receipt"
    ]
    assert plan["proposal"]["proposal_ref"].startswith("exomem://proposal/")
    assert plan["next_step"].startswith("Review outline_markdown")

    [source] = plan["sources"]
    assert source["original_path"] == "Warranty Case/laptop-receipt.md"
    assert source["original_ref"] == "exomem://vault/Warranty%20Case/laptop-receipt.md"
    assert source["source_path"] == "Knowledge Base/Sources/Imported/2026-07-07-laptop-receipt.md"
    assert source["source_ref"] == (
        "exomem://source/Knowledge%20Base/Sources/Imported/2026-07-07-laptop-receipt"
    )
    assert source["already_governed"] is False


def test_adopt_compile_selected_requires_explicit_selection(tmp_path: Path) -> None:
    with pytest.raises(adopt_module.AdoptError) as ei:
        adopt_module.adopt(_legacy_vault(tmp_path, kb=True), mode="compile-selected")
    assert ei.value.code == "MISSING_SELECTION"


def test_adopt_compile_selected_skips_unsupported_without_writing(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=True)
    (vault / "scan.jpg").write_bytes(b"not really an image")
    before = _snapshot(vault)

    report = adopt_module.adopt(
        vault,
        mode="compile-selected",
        selected_paths=["scan.jpg"],
        today=dt.date(2026, 7, 7),
    )

    assert _snapshot(vault) == before
    plan = report["compile_plan"]
    assert plan["status"] == "empty"
    assert plan["proposal"] is None
    assert plan["skipped"] == [
        {
            "path": "scan.jpg",
            "code": "UNSUPPORTED_IMPORT_TYPE",
            "reason": "compile-selected currently imports text/markdown-like files only",
            "ref": "exomem://vault/scan.jpg",
        }
    ]

def test_adopt_copy_as_sources_requires_explicit_selection(tmp_path: Path) -> None:
    with pytest.raises(adopt_module.AdoptError) as ei:
        adopt_module.adopt(_legacy_vault(tmp_path, kb=True), mode="copy-as-sources")
    assert ei.value.code == "MISSING_SELECTION"


def test_adopt_unsupported_mode_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(adopt_module.AdoptError) as ei:
        adopt_module.adopt(_legacy_vault(tmp_path), mode="teleport")
    assert ei.value.code == "UNSUPPORTED_MODE"
    assert "supported modes" in ei.value.reason
    assert "compile-selected" in ei.value.reason


def test_pack_suggestions_default_to_personal_records() -> None:
    out = knowledge_packs.suggest_packs({"tree": []})
    assert out[0]["id"] == "personal-records"
    assert out[0]["score"] == 0


def test_pack_validation_rejects_unknown_fields() -> None:
    raw = knowledge_packs.list_builtin_packs()[0]
    raw["surprise"] = True
    with pytest.raises(knowledge_packs.PackValidationError) as ei:
        knowledge_packs.validate_pack_dict(raw)
    assert ei.value.code == "UNKNOWN_FIELD"
def test_pack_validation_rejects_invalid_workflows() -> None:
    raw = knowledge_packs.list_builtin_packs()[0]
    raw["suggested_workflows"] = [{"title": "Missing route", "intent": "x", "example": "x"}]
    with pytest.raises(knowledge_packs.PackValidationError) as ei:
        knowledge_packs.validate_pack_dict(raw)
    assert ei.value.code == "MISSING_WORKFLOW_FIELD"

    raw = knowledge_packs.list_builtin_packs()[0]
    raw["default_note_types"] = []
    with pytest.raises(knowledge_packs.PackValidationError) as ei:
        knowledge_packs.validate_pack_dict(raw)
    assert ei.value.code == "INVALID_FIELD"


def test_selected_pack_manifest_roundtrip(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path, kb=True)

    written = knowledge_packs.write_selected_packs(
        vault,
        ["technical", "creative", "technical"],
        source="test",
        today=dt.date(2026, 7, 7),
    )
    state = knowledge_packs.selected_pack_state(vault)

    assert written["path"] == "Knowledge Base/_Packs/selected-packs.json"
    assert written["selected_pack_ids"] == ["technical", "creative"]
    assert state["manifest_present"] is True
    assert state["selected_pack_ids"] == ["technical", "creative"]
    assert state["packs"][0]["agent_instructions"]


def test_builtin_packs_are_declarative_files() -> None:
    base = resources.files("exomem").joinpath("packs")
    names = sorted(entry.name for entry in base.iterdir() if entry.name.endswith(".json"))

    assert names == [
        "business.json",
        "creative.json",
        "health-athletic.json",
        "legal-warranty.json",
        "personal-records.json",
        "technical.json",
    ]
    raw = json.loads(base.joinpath("legal-warranty.json").read_text(encoding="utf-8"))
    assert knowledge_packs.validate_pack_dict(raw).id == "legal-warranty"
    assert knowledge_packs.pack_schema()["directory"] == "src/exomem/packs/"


def test_pack_validation_rejects_invalid_primitives_and_actions() -> None:
    raw = knowledge_packs.list_builtin_packs()[0]
    raw["primitives"] = ["source", "mind-palace"]
    with pytest.raises(knowledge_packs.PackValidationError) as ei:
        knowledge_packs.validate_pack_dict(raw)
    assert ei.value.code == "INVALID_PRIMITIVE"

    raw = knowledge_packs.list_builtin_packs()[0]
    raw["actions"] = ["save", "teleport"]
    with pytest.raises(knowledge_packs.PackValidationError) as ei:
        knowledge_packs.validate_pack_dict(raw)
    assert ei.value.code == "INVALID_ACTION"


def test_adopt_registry_exposure_survives_tier2_optout() -> None:
    from exomem.commands import product_commands_for

    for surface in ("mcp", "cli", "rest"):
        commands = {c.name: c for c in product_commands_for(surface, expose_tier2=False)}
        assert "adopt_vault" in commands, f"adopt_vault missing from {surface} with Tier 2 off"
        assert commands["adopt_vault"].product_surface == "primary"
        assert "adopt" in commands["adopt_vault"].product_actions


def test_adopt_cli_door(vault: Path, capsys) -> None:
    code = main(["adopt", "--json"])
    out = capsys.readouterr().out

    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert payload["data"]["mode"] == "scan-only"
    assert payload["data"]["summary"]["kb"]["present"] is True


def test_product_cli_scan_only_adoption_allows_pre_init_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))

    code = main(["adopt_vault", ".", "--mode", "scan-only", "--json"])
    out = capsys.readouterr().out

    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert payload["data"]["mode"] == "scan-only"
    assert payload["data"]["summary"]["kb"]["present"] is False


def test_product_cli_browse_memory_allows_pre_init_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))

    code = main(["browse_memory", ".", "--mode", "overview", "--json"])
    out = capsys.readouterr().out

    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    assert payload["data"]["kb"]["present"] is False
    assert payload["data"]["totals"]["markdown"] >= 3


def test_product_cli_write_adoption_still_requires_initialized_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    vault = _legacy_vault(tmp_path, kb=False)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))

    code = main(["adopt_vault", ".", "--mode", "copy-as-sources", "--json"])
    out = capsys.readouterr().out

    assert code == 1
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is False
    assert "does not look like a vault" in payload["error"]["message"]


def test_adopt_cli_human_output_is_product_shaped(vault: Path, capsys) -> None:
    code = main(["adopt"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Adoption report" in out
    assert "Likely packs" in out
    assert "Safe next actions" in out
    assert "Originals: untouched" in out
    assert '"mode"' not in out
