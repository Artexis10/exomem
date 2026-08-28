from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY_MARKER = "<!-- spec-system:openspec-only -->"


def test_repository_uses_openspec_as_its_only_specification_system() -> None:
    for instruction_file in ("AGENTS.md", "CLAUDE.md"):
        instructions = (ROOT / instruction_file).read_text(encoding="utf-8")
        assert POLICY_MARKER in instructions, (
            f"{instruction_file} must declare the OpenSpec-only policy"
        )

    assert not (ROOT / "docs" / "superpowers").exists(), (
        "legacy Superpowers specifications must be migrated into OpenSpec, "
        "then removed"
    )
