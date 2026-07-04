"""`exomem personalize` — scan a vault, classify sibling folders, generate _access.yaml.

Pure-function tests feed synthetic `overview` dicts; integration tests build a tmp vault
and assert the emitted file is honored by the real `access` layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from exomem import access as access_module
from exomem import personalize as p
from exomem.__main__ import main


def _scan(*folders, junk=None, children_omitted=0) -> dict:
    """Build a minimal `overview`-shaped dict. Each folder: {"path","files","md","binary"}."""
    tree = [
        {"path": "", "depth": 0, "files_recursive": 0, "markdown": 0, "binary": 0,
         "children_omitted": children_omitted}
    ]
    for f in folders:
        tree.append({
            "path": f["path"], "depth": 1,
            "files_recursive": f.get("files", 0),
            "markdown": f.get("md", 0),
            "binary": f.get("binary", 0),
            "children_omitted": 0,
        })
    junk = junk or {}
    return {
        "tree": tree,
        "junk": {
            "sync_conflicts": junk.get("sync_conflicts", []),
            "zero_byte": junk.get("zero_byte", []),
        },
    }


_EMPTY_POLICY = {"readonly": [], "excluded": []}


# --- classifier (pure) -----------------------------------------------------


def test_markdown_sibling_defaults_readonly() -> None:
    props = p.classify_siblings(_scan({"path": "Reference", "files": 5, "md": 5}), _EMPTY_POLICY)
    assert [(x.folder, x.classification) for x in props] == [("Reference", p.CLASS_READONLY)]


def test_binary_heavy_no_markdown_excluded() -> None:
    props = p.classify_siblings(_scan({"path": "Photos", "files": 10, "md": 0, "binary": 10}), _EMPTY_POLICY)
    assert props[0].classification == p.CLASS_EXCLUDED


def test_junk_dominant_excluded() -> None:
    scan = _scan(
        {"path": "Sync", "files": 4, "md": 2},
        junk={"sync_conflicts": ["Sync/a.md", "Sync/b.md"]},
    )
    assert p.classify_siblings(scan, _EMPTY_POLICY)[0].classification == p.CLASS_EXCLUDED


def test_empty_folder_unmanaged() -> None:
    props = p.classify_siblings(_scan({"path": "Empty", "files": 0}), _EMPTY_POLICY)
    assert props[0].classification == p.CLASS_UNMANAGED


def test_knowledge_base_never_proposed() -> None:
    scan = _scan(
        {"path": "Knowledge Base", "files": 3, "md": 3},
        {"path": "Reference", "files": 2, "md": 2},
    )
    assert [x.folder for x in p.classify_siblings(scan, _EMPTY_POLICY)] == ["Reference"]


def test_already_configured_not_reproposed() -> None:
    scan = _scan({"path": "Reference", "files": 2, "md": 2})
    props = p.classify_siblings(scan, {"readonly": ["Reference"], "excluded": []})
    assert props[0].already_configured == p.CLASS_READONLY


# --- merge (pure, byte-stable) ---------------------------------------------


def test_merge_fresh_file() -> None:
    text = p.merge_access_yaml(None, ["Reference", "Daily"], ["Photos"])
    assert yaml.safe_load(text) == {"readonly": ["Daily", "Reference"], "excluded": ["Photos"]}
    assert "# exomem access policy" in text


def test_merge_preserves_existing_and_unknown_keys() -> None:
    existing = "readonly:\n- Existing\ncustom_key: hello\n"
    loaded = yaml.safe_load(p.merge_access_yaml(existing, ["New"], []))
    assert set(loaded["readonly"]) == {"Existing", "New"}
    assert loaded["custom_key"] == "hello"


def test_merge_idempotent() -> None:
    t1 = p.merge_access_yaml(None, ["A", "B"], ["C"])
    assert p.merge_access_yaml(t1, ["A", "B"], ["C"]) == t1


# --- integration (tmp vault, honored by real access layer) -----------------


def _vault_with_kb(tmp_path: Path, *, reference: bool = True, photos: bool = False) -> Path:
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    if reference:
        (vault / "Reference").mkdir()
        (vault / "Reference" / "note.md").write_text("# note\n", encoding="utf-8")
    if photos:
        (vault / "Photos").mkdir()
        (vault / "Photos" / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    return vault


def test_personalize_writes_and_access_honors(tmp_path: Path) -> None:
    vault = _vault_with_kb(tmp_path, reference=True, photos=True)
    report = p.personalize(vault, apply=True)
    assert report.wrote
    assert (vault / "Knowledge Base" / "_access.yaml").is_file()
    # The load-bearing assertion: the real access layer understands what we wrote.
    assert access_module.access_tier(vault, "Reference/note.md") == access_module.TIER_READONLY
    assert access_module.access_tier(vault, "Photos/pic.png") == access_module.TIER_EXCLUDED


def test_personalize_dry_run_writes_nothing(tmp_path: Path) -> None:
    vault = _vault_with_kb(tmp_path)
    report = p.personalize(vault, apply=False)
    assert report.proposals and report.wrote is False
    assert not (vault / "Knowledge Base" / "_access.yaml").exists()


def test_personalize_rerun_unchanged(tmp_path: Path) -> None:
    vault = _vault_with_kb(tmp_path, reference=True, photos=True)
    p.personalize(vault, apply=True)
    cfg = vault / "Knowledge Base" / "_access.yaml"
    before = cfg.read_bytes()
    report2 = p.personalize(vault, apply=True)
    assert report2.unchanged and report2.wrote is False
    assert cfg.read_bytes() == before


def test_personalize_requires_kb(tmp_path: Path) -> None:
    with pytest.raises(p.PersonalizeError) as ei:
        p.scan_and_classify(tmp_path / "no-vault")
    assert ei.value.code == "NO_KB"


def test_cap_omitted_surfaced(tmp_path: Path) -> None:
    vault = _vault_with_kb(tmp_path)
    scan = _scan({"path": "Reference", "files": 2, "md": 2}, children_omitted=3)
    report = p.scan_and_classify(vault, overview_fn=lambda _v: scan)
    assert report.cap_omitted == 3


# --- CLI + interactive -----------------------------------------------------


def test_run_personalize_yes_applies(tmp_path: Path) -> None:
    vault = _vault_with_kb(tmp_path)
    lines: list[str] = []
    code = p.run_personalize(
        vault=str(vault), yes=True,
        input_fn=lambda prompt="": pytest.fail(f"unexpected prompt: {prompt}"),
        print_fn=lines.append,
    )
    assert code == 0
    assert (vault / "Knowledge Base" / "_access.yaml").is_file()


def test_run_personalize_declined(tmp_path: Path) -> None:
    vault = _vault_with_kb(tmp_path)
    code = p.run_personalize(vault=str(vault), yes=False, input_fn=lambda prompt="": "n", print_fn=lambda *_: None)
    assert code == 0
    assert not (vault / "Knowledge Base" / "_access.yaml").exists()


def test_personalize_dispatches_from_main(tmp_path: Path) -> None:
    vault = _vault_with_kb(tmp_path)
    assert main(["personalize", "--vault", str(vault), "--yes"]) == 0
    assert (vault / "Knowledge Base" / "_access.yaml").is_file()


def test_yes_without_vault_is_usage_error() -> None:
    with pytest.raises(SystemExit) as ei:
        main(["personalize", "--yes"])
    assert ei.value.code == 2
