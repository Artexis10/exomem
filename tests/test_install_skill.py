"""install-skill — copy the bundled exomem skill into Claude Code.

The MCP server is only the hands (find/add/note); the skill is the brain that
tells Claude when to capture and how to file. Until it's installed at
`~/.claude/skills/exomem/SKILL.md`, the tools sit unused — so installing
it straight from the package (no vault round-trip) is a first-class operation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import install_skill as install_module
from exomem import semantic_authoring
from exomem.public_artifact_privacy import assert_public_artifacts_clean

EXPECTED_WORKFLOW_SKILLS = [
    "exomem-continue",
    "exomem-capture",
    "exomem-ingest",
    "exomem-research",
    "exomem-reflect",
    "exomem-curate",
    "exomem-defrag",
    "exomem-review",
    "exomem-media",
]


def test_install_skill_copies_into_target(tmp_path: Path) -> None:
    target = tmp_path / "exomem"
    report = install_module.install_skill(target)

    # SKILL.md + references/ land at the target root — Claude Code discovers a
    # skill by <target>/SKILL.md.
    assert (target / "SKILL.md").exists()
    assert (target / "references").is_dir()
    assert (target / "references" / "operations.md").exists()
    assert (target / "workflow-skills" / "index.yaml").exists()

    workflow_names = [s["name"] for s in report["workflow_skills"]]
    assert workflow_names == EXPECTED_WORKFLOW_SKILLS
    for name in EXPECTED_WORKFLOW_SKILLS:
        assert (tmp_path / name / "SKILL.md").exists()

    assert report["target"] == str(target)
    assert report["mode"] == "copy"
    assert report["files"] > 0


def test_install_skill_populates_empty_dir(tmp_path: Path) -> None:
    """An empty target dir (common — a parent mkdir often leaves one) is safe to
    fill without --force."""
    target = tmp_path / "exomem"
    target.mkdir()
    install_module.install_skill(target)
    assert (target / "SKILL.md").exists()


def test_install_skill_refuses_existing_without_force(tmp_path: Path) -> None:
    target = tmp_path / "exomem"
    target.mkdir()
    (target / "SKILL.md").write_text("stale", encoding="utf-8")  # non-empty
    with pytest.raises(FileExistsError):
        install_module.install_skill(target)


def test_install_skill_force_overwrites_cleanly(tmp_path: Path) -> None:
    target = tmp_path / "exomem"
    target.mkdir()
    (target / "stale.md").write_text("old", encoding="utf-8")

    install_module.install_skill(target, force=True)

    # A faithful mirror: canonical SKILL.md present, stale leftovers gone.
    assert (target / "SKILL.md").exists()
    assert not (target / "stale.md").exists()
    assert (tmp_path / "exomem-review" / "SKILL.md").exists()


def test_install_skill_via_cli(tmp_path: Path) -> None:
    """`python -m exomem install-skill --target <path>` installs and returns 0;
    a second run refuses (1) without --force, then succeeds (0) with it."""
    from exomem.__main__ import main

    target = tmp_path / "exomem"
    assert main(["install-skill", "--target", str(target)]) == 0
    assert (target / "SKILL.md").exists()
    assert main(["install-skill", "--target", str(target)]) == 1
    assert main(["install-skill", "--target", str(target), "--force"]) == 0


def test_remove_legacy_skill_retires_ours(tmp_path: Path) -> None:
    """A pre-rename install (old `name: knowledge-base` marker + the Exomem
    fingerprint) is removed."""
    legacy = tmp_path / "skills" / "knowledge-base"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(
        "---\nname: knowledge-base\n---\n\nThis skill is the Exomem contract.\n",
        encoding="utf-8",
    )
    removed = install_module.remove_legacy_skill(legacy)
    assert removed == legacy
    assert not legacy.exists()


def test_remove_legacy_skill_preserves_foreign(tmp_path: Path) -> None:
    """A user's own skill that merely shares the legacy folder name (no old marker) is
    left untouched."""
    legacy = tmp_path / "skills" / "knowledge-base"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("---\nname: my-custom-skill\n---\n", encoding="utf-8")
    assert install_module.remove_legacy_skill(legacy) is None
    assert (legacy / "SKILL.md").exists()


def test_remove_legacy_skill_absent_is_noop(tmp_path: Path) -> None:
    assert install_module.remove_legacy_skill(tmp_path / "skills" / "knowledge-base") is None


# ---- multi-client: Codex loads skills from disk exactly like Claude Code ----
#
# install_hook.py has always targeted both clients; skills never did, so a Codex
# user got the MCP tools and no brain to drive them.


def test_client_home_honours_the_same_env_overrides_as_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    assert install_module.client_home("claude") == tmp_path / "claude"
    assert install_module.client_home("codex") == tmp_path / "codex"
    assert install_module.client_skill_target("codex") == tmp_path / "codex" / "skills" / "exomem"


def test_unsupported_client_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported skill client"):
        install_module.normalize_client("cursor")


def test_auto_resolves_only_to_clients_present_on_this_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "codex").mkdir()

    assert install_module.resolve_clients("auto") == ("codex",)
    # 'all' provisions ahead of time, whether or not the client is installed yet.
    assert install_module.resolve_clients("all") == ("claude", "codex")


def test_auto_falls_back_to_claude_when_no_client_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first-run box with neither config dir must still install somewhere useful."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    assert install_module.resolve_clients("auto") == ("claude",)


def test_install_skills_lands_the_full_set_in_both_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    report = install_module.install_skills(client="all")

    assert report["installed"] == ["claude", "codex"]
    for client in ("claude", "codex"):
        skills = tmp_path / client / "skills"
        assert (skills / "exomem" / "SKILL.md").is_file()
        for name in EXPECTED_WORKFLOW_SKILLS:
            assert (skills / name / "SKILL.md").is_file(), f"{name} missing for {client}"


def test_clean_filesystem_install_is_self_sufficient_without_personal_skills(
    tmp_path: Path,
) -> None:
    target = tmp_path / "empty-client" / "skills" / "exomem"
    install_module.install_skill(target)
    concise = semantic_authoring.render_concise()

    core = (target / "SKILL.md").read_text(encoding="utf-8")
    assert concise in core
    assert (target / "references" / "page-types.md").is_file()
    for name in EXPECTED_WORKFLOW_SKILLS:
        standalone = target.parent / name / "SKILL.md"
        assert concise in standalone.read_text(encoding="utf-8")
    installed_files = [path for path in target.parent.rglob("*") if path.is_file()]
    assert_public_artifacts_clean(
        installed_files,
        labels={
            path: f"filesystem-install/{path.relative_to(target.parent).as_posix()}"
            for path in installed_files
        },
    )
