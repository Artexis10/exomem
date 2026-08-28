from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY_MARKER = "<!-- spec-system:openspec-only -->"
NON_SPEC_MARKER = "<!-- authority:non-specification -->"


def test_repository_uses_openspec_as_its_only_specification_system() -> None:
    for instruction_file in ("AGENTS.md", "CLAUDE.md"):
        instructions = (ROOT / instruction_file).read_text(encoding="utf-8")
        assert POLICY_MARKER in instructions, (
            f"{instruction_file} must declare the OpenSpec-only policy"
        )
        normalized = instructions.casefold()
        for phrase in (
            "sole specification system",
            "routine restorative fixes",
            "migrate any unique durable contract",
        ):
            assert phrase in normalized, f"{instruction_file} lost policy: {phrase}"

    for forbidden_root in (
        ROOT / "docs" / "superpowers",
        ROOT / "docs" / "specs",
        ROOT / "docs" / "plans",
    ):
        assert not forbidden_root.exists(), (
            f"parallel specification root must not exist: {forbidden_root}"
        )


def test_every_docs_markdown_file_is_explicitly_non_authoritative() -> None:
    offenders = []
    for document in (ROOT / "docs").rglob("*.md"):
        text = document.read_text(encoding="utf-8")
        if NON_SPEC_MARKER not in text:
            offenders.append(document.relative_to(ROOT).as_posix())

    assert offenders == []


def test_migrated_connector_and_init_recovery_contracts_are_explicit() -> None:
    command_surface = (
        ROOT / "openspec" / "specs" / "command-surface" / "spec.md"
    ).read_text(encoding="utf-8")
    for phrase in (
        "SHALL also accept connector-supplied JSON-object strings",
        "JSON-object strings",
        "INVALID_EDIT",
        "decoded text still passes the binary-blob guard",
    ):
        assert phrase in command_surface

    hosted = (
        ROOT
        / "openspec"
        / "changes"
        / "add-hosted-private-alpha-infrastructure"
        / "specs"
        / "hosted-cell-orchestration"
        / "spec.md"
    ).read_text(encoding="utf-8")
    for phrase in (
        "init Job is absent",
        "running or reports `Complete=True`",
        "Failed-only init Job",
        "terminating init Job",
    ):
        assert phrase in hosted
