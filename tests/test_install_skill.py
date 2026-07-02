"""install-skill — copy the bundled knowledge-base skill into Claude Code.

The MCP server is only the hands (find/add/note); the skill is the brain that
tells Claude when to capture and how to file. Until it's installed at
`~/.claude/skills/knowledge-base/SKILL.md`, the tools sit unused — so installing
it straight from the package (no vault round-trip) is a first-class operation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import install_skill as install_module


def test_install_skill_copies_into_target(tmp_path: Path) -> None:
    target = tmp_path / "knowledge-base"
    report = install_module.install_skill(target)

    # SKILL.md + references/ land at the target root — Claude Code discovers a
    # skill by <target>/SKILL.md.
    assert (target / "SKILL.md").exists()
    assert (target / "references").is_dir()
    assert (target / "references" / "operations.md").exists()

    assert report["target"] == str(target)
    assert report["mode"] == "copy"
    assert report["files"] > 0


def test_install_skill_populates_empty_dir(tmp_path: Path) -> None:
    """An empty target dir (common — a parent mkdir often leaves one) is safe to
    fill without --force."""
    target = tmp_path / "knowledge-base"
    target.mkdir()
    install_module.install_skill(target)
    assert (target / "SKILL.md").exists()


def test_install_skill_refuses_existing_without_force(tmp_path: Path) -> None:
    target = tmp_path / "knowledge-base"
    target.mkdir()
    (target / "SKILL.md").write_text("stale", encoding="utf-8")  # non-empty
    with pytest.raises(FileExistsError):
        install_module.install_skill(target)


def test_install_skill_force_overwrites_cleanly(tmp_path: Path) -> None:
    target = tmp_path / "knowledge-base"
    target.mkdir()
    (target / "stale.md").write_text("old", encoding="utf-8")

    install_module.install_skill(target, force=True)

    # A faithful mirror: canonical SKILL.md present, stale leftovers gone.
    assert (target / "SKILL.md").exists()
    assert not (target / "stale.md").exists()


def test_install_skill_via_cli(tmp_path: Path) -> None:
    """`python -m exomem install-skill --target <path>` installs and returns 0;
    a second run refuses (1) without --force, then succeeds (0) with it."""
    from exomem.__main__ import main

    target = tmp_path / "knowledge-base"
    assert main(["install-skill", "--target", str(target)]) == 0
    assert (target / "SKILL.md").exists()
    assert main(["install-skill", "--target", str(target)]) == 1
    assert main(["install-skill", "--target", str(target), "--force"]) == 0
