"""Empty-policy fast path, corpus hygiene, and the internal read-leaf registry.

Zero enforcement this change: nothing here is wired into `find`/`get`/
`overview`/`graph`/`read_media`/query — this file only pins (a) the
empty-policy short circuit never touches the sidecar, (b) `_Governance/`
never surfaces as indexable content, and (c) the kernel's internal read
leaves are registry-level plumbing, not a shipped user-facing tool.
"""

from __future__ import annotations

from pathlib import Path

from exomem import commands, governance
from exomem import find as find_module
from exomem.governance import store


def test_empty_policy_short_circuit(vault: Path) -> None:
    result = governance.decide_paths(
        vault, ["Knowledge Base/Notes/anything.md"], audience="external"
    )
    assert result["Knowledge Base/Notes/anything.md"].level == governance.DISCLOSURE_MAX
    assert not store.sidecar_path(vault).exists()


def test_blocked_policy_short_circuits_to_fail_closed_floor(vault: Path) -> None:
    """A cold-start compile refusal (no prior good compile) must resolve to a
    fail-closed L0 floor for every requested path — never the open fast path.
    Reproduces the reviewer's finding: a plain `ceiling: 9` typo on a fresh
    process previously made `decide_paths` return full disclosure (L6)."""
    scope = vault / "Knowledge Base" / "_Governance" / "scopes" / "acmeco.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        "paths: [\"Projects/AcmeCo/**\"]\n",
        encoding="utf-8",
    )
    rule = vault / "Knowledge Base" / "_Governance" / "rules" / "acmeco-external.yaml"
    rule.parent.mkdir(parents=True, exist_ok=True)
    rule.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\n"
        "audience: external\nceiling: 9\n",  # plain typo, out of range
        encoding="utf-8",
    )

    result = governance.decide_paths(
        vault, ["Knowledge Base/Notes/anything.md"], audience="external"
    )
    assert result["Knowledge Base/Notes/anything.md"].level == governance.DISCLOSURE_MIN
    assert not store.sidecar_path(vault).exists()


def test_governance_dir_never_surfaces_in_find(vault: Path) -> None:
    planted = vault / "Knowledge Base" / "_Governance" / "scopes" / "leaked-note.yaml"
    planted.parent.mkdir(parents=True, exist_ok=True)
    # Not even valid policy YAML — just a markdown-shaped file that COULD be
    # picked up as content if `_Governance/` weren't excluded from the walk.
    stray_md = vault / "Knowledge Base" / "_Governance" / "scopes" / "leaked-note.md"
    stray_md.write_text(
        "---\ntype: source\n---\nqzxvv-governance-leak-marker\n", encoding="utf-8"
    )
    find_module.clear_cache()
    hits = find_module.find(vault, query="qzxvv-governance-leak-marker")
    assert not any("leaked-note" in h.path for h in hits)


def test_kernel_leaves_are_registered_internally_only() -> None:
    expected = {
        "governance.load",
        "governance.evaluate_membership",
        "governance.decide",
        "governance.decide_paths",
    }
    assert set(governance.KERNEL_LEAVES) == expected
    for leaf in governance.KERNEL_LEAVES.values():
        assert callable(leaf)


def test_no_user_facing_command_ships_this_change() -> None:
    """No governance-named tool/operation is registered on the command
    surface — the kernel lands as inspection-only, internal plumbing."""
    command_names = {cmd.name for cmd in commands.COMMANDS}
    assert not any("governance" in name for name in command_names)
