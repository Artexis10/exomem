"""The `overview` op: bounded, read-only vault-structure report.

Core-function tests build a messy tmp vault (no `Knowledge Base/`) to prove the
pre-init path; the CLI door runs against the fixture vault. The report must be
deterministic, bounded, and side-effect free.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import overview as overview_module
from exomem.__main__ import main


def _build_messy_vault(root: Path) -> None:
    """A daily-notes-shaped vault: no KB, no frontmatter, junk included."""
    daily = root / "Daily" / "2026" / "2026-1"
    daily.mkdir(parents=True)
    (daily / "2026-01-05.md").write_text("- 09:00-10:00 **60** deep work\n", encoding="utf-8")
    (daily / "2026-01-06.md").write_text("- 10:00-11:30 **90** review\n", encoding="utf-8")
    (daily / "2026-01-06 2.md").write_text("x", encoding="utf-8")  # sync-conflict copy
    memo = root / "Memo"
    memo.mkdir()
    (memo / "note.md").write_text(
        "---\ntype: note\n---\nbody [[Daily/2026-01-05]] and [ext](https://example.com)\n",
        encoding="utf-8",
    )
    (memo / "note 2.md").write_text("conflict copy\n", encoding="utf-8")
    (root / "empty.md").write_text("", encoding="utf-8")  # zero-byte
    assets = root / "assets"
    assets.mkdir()
    (assets / "pic.png").write_bytes(b"\x89PNG\r\n" + b"\x00" * 64)
    hidden = root / ".obsidian"
    hidden.mkdir()
    (hidden / "app.json").write_text("{}", encoding="utf-8")
    deep = root / "deep" / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "leaf.md").write_text("deep leaf\n", encoding="utf-8")


@pytest.fixture
def messy(tmp_path: Path) -> Path:
    root = tmp_path / "messy-vault"
    root.mkdir()
    _build_messy_vault(root)
    return root


def _snapshot(root: Path) -> dict[str, tuple[int, float]]:
    return {
        p.as_posix(): (p.stat().st_size, p.stat().st_mtime)
        for p in root.rglob("*")
        if p.is_file()
    }


def test_report_on_messy_vault(messy: Path) -> None:
    report = overview_module.overview(messy)
    # 8 visible files: 3 daily + 2 memo + empty.md + pic.png + deep leaf
    # (.obsidian/app.json is skipped)
    assert report["totals"]["files"] == 8
    assert report["totals"]["markdown"] == 7
    assert report["totals"]["binary"] == 1
    assert report["kb"] == {"present": False}
    assert ".obsidian" in report["skipped"]["dirs"]
    # junk: exact detection with exact counts
    assert report["junk"]["counts"] == {"zero_byte": 1, "sync_conflicts": 2}
    assert "empty.md" in report["junk"]["zero_byte"]
    assert "Daily/2026/2026-1/2026-01-06 2.md" in report["junk"]["sync_conflicts"]
    assert "Memo/note 2.md" in report["junk"]["sync_conflicts"]
    # frontmatter coverage: only Memo/note.md carries frontmatter (1 of 7 md)
    root_entry = next(e for e in report["tree"] if e["path"] == "")
    assert root_entry["frontmatter_pct"] == pytest.approx(100 * 1 / 7, abs=0.1)
    assert root_entry["wikilinks"] == 1
    assert root_entry["md_links"] == 1
    # all reported paths are POSIX
    assert not any("\\" in e["path"] for e in report["tree"])


def test_naming_patterns_surface_daily_convention(messy: Path) -> None:
    report = overview_module.overview(messy)
    daily = next(e for e in report["tree"] if e["path"] == "Daily/2026/2026-1")
    patterns = {p["pattern"]: p["count"] for p in daily["name_patterns"]}
    assert patterns.get("NNNN-NN-NN.md") == 2


def test_depth_cap_rolls_up_without_losing_counts(messy: Path) -> None:
    report = overview_module.overview(messy, max_depth=2)
    assert all(e["depth"] <= 2 for e in report["tree"])
    deep = next(e for e in report["tree"] if e["path"] == "deep")
    assert deep["files_recursive"] == 1  # deep/a/b/c/leaf.md rolled up, still counted
    assert deep["children_omitted"] >= 0
    assert report["totals"]["files"] == 8  # totals never change with caps


def test_breadth_cap_marks_omissions_with_exact_totals(tmp_path: Path) -> None:
    root = tmp_path / "wide"
    root.mkdir()
    for i in range(overview_module.BREADTH_CAP + 8):
        d = root / "many" / f"topic-{i:02d}"
        d.mkdir(parents=True)
        (d / "note.md").write_text("x\n", encoding="utf-8")
    report = overview_module.overview(root)
    many = next(e for e in report["tree"] if e["path"] == "many")
    shown_children = [e for e in report["tree"] if e["path"].startswith("many/")]
    assert len(shown_children) == overview_module.BREADTH_CAP
    assert many["children_omitted"] == 8
    assert many["files_recursive"] == overview_module.BREADTH_CAP + 8


def test_oversized_markdown_is_counted_not_read(tmp_path: Path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    (root / "big.md").write_text("---\nx: 1\n---\n" + "a" * (overview_module.CONTENT_READ_CAP + 10), encoding="utf-8")
    (root / "small.md").write_text("plain\n", encoding="utf-8")
    report = overview_module.overview(root)
    assert report["skipped"]["oversized_files"] == 1
    root_entry = next(e for e in report["tree"] if e["path"] == "")
    # coverage computed over readable md only: small.md has no frontmatter
    assert root_entry["frontmatter_pct"] == 0.0


def test_deterministic_and_read_only(messy: Path) -> None:
    before = _snapshot(messy)
    first = overview_module.overview(messy)
    second = overview_module.overview(messy)
    assert first == second
    assert _snapshot(messy) == before


def test_kb_detected_when_present(messy: Path) -> None:
    kb = messy / "Knowledge Base"
    (kb / "Notes").mkdir(parents=True)
    (kb / "index.md").write_text("# index\n", encoding="utf-8")
    report = overview_module.overview(messy)
    assert report["kb"]["present"] is True
    assert report["kb"]["files"] == 1


def test_subtree_scan_and_errors(messy: Path) -> None:
    report = overview_module.overview(messy, path="Daily")
    assert report["totals"]["files"] == 3
    with pytest.raises(overview_module.OverviewError) as e:
        overview_module.overview(messy, path="nope")
    assert e.value.code == "NOT_FOUND"
    with pytest.raises(overview_module.OverviewError) as e:
        overview_module.overview(messy, path="../outside")
    assert e.value.code == "INVALID_PATH"
    with pytest.raises(overview_module.OverviewError) as e:
        overview_module.overview(messy, path="empty.md")
    assert e.value.code == "NOT_A_DIR"


def test_registry_exposure_survives_tier2_optout() -> None:
    from exomem.commands import commands_for

    for surface in ("mcp", "cli", "rest"):
        names = {c.name for c in commands_for(surface, expose_tier2=False)}
        assert "overview" in names, f"overview missing from {surface} with Tier 2 off"


def _run(argv: list[str], capsys) -> tuple[int, str]:
    try:
        code = main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return code, capsys.readouterr().out


def test_cli_door(vault: Path, capsys) -> None:
    code, out = _run(["overview", "--json"], capsys)
    assert code == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["success"] is True
    data = payload["data"]
    assert data["kb"]["present"] is True
    assert data["totals"]["files"] > 0
    assert data["scope_note"]
