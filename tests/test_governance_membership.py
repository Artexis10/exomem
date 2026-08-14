"""Query-time scope membership: each selector kind, excludes, memo invalidation.

Membership is evaluated against an already-parsed `ParsedPage` (design D4) —
never an index-time table — and memoized per `(fingerprint, path, mtime_ns)`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from exomem import find_corpus
from exomem.governance import membership, policy


def _write_scope(vault: Path, name: str, text: str) -> Path:
    p = vault / "Knowledge Base" / "_Governance" / "scopes" / f"{name}.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _write_page(vault: Path, rel_path: str, frontmatter: str, body: str = "body") -> Path:
    p = vault / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")
    return p


def _parse(vault: Path, rel_path: str):
    p = vault / rel_path
    return find_corpus.parse_page(p, p.stat().st_mtime, vault)


def test_path_glob_selector_resolves_membership(vault: Path) -> None:
    _write_scope(
        vault,
        "acmeco",
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: [\"Knowledge Base/Projects/AcmeCo/**\"]\n",
    )
    pol = policy.load(vault)
    _write_page(vault, "Knowledge Base/Projects/AcmeCo/client-notes.md", "type: source")
    parsed = _parse(vault, "Knowledge Base/Projects/AcmeCo/client-notes.md")
    assert "01ARZ3NDEKTSV4RRFFQ69G5FAV" in membership.evaluate(parsed, pol)

    _write_page(vault, "Knowledge Base/Notes/other.md", "type: source")
    parsed_outside = _parse(vault, "Knowledge Base/Notes/other.md")
    assert membership.evaluate(parsed_outside, pol) == frozenset()


def test_project_selector_resolves_membership(vault: Path) -> None:
    _write_scope(
        vault,
        "acmeco",
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nprojects: [\"acmeco\"]\n",
    )
    pol = policy.load(vault)
    _write_page(vault, "Knowledge Base/Notes/x.md", "type: insight\nproject: acmeco")
    parsed = _parse(vault, "Knowledge Base/Notes/x.md")
    assert "01ARZ3NDEKTSV4RRFFQ69G5FAV" in membership.evaluate(parsed, pol)


def test_tag_selector_resolves_membership(vault: Path) -> None:
    _write_scope(
        vault,
        "confidential",
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB1\ntags: [\"confidential\"]\n",
    )
    pol = policy.load(vault)
    _write_page(vault, "Knowledge Base/Notes/x.md", "type: insight\ntags: [confidential]")
    parsed = _parse(vault, "Knowledge Base/Notes/x.md")
    assert "01ARZ3NDEKTSV4RRFFQ69G5FB1" in membership.evaluate(parsed, pol)


def test_type_selector_resolves_membership(vault: Path) -> None:
    _write_scope(
        vault,
        "sources",
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB2\ntypes: [\"source\"]\n",
    )
    pol = policy.load(vault)
    _write_page(vault, "Knowledge Base/Sources/x.md", "type: source")
    parsed = _parse(vault, "Knowledge Base/Sources/x.md")
    assert "01ARZ3NDEKTSV4RRFFQ69G5FB2" in membership.evaluate(parsed, pol)


def test_class_selector_resolves_membership(vault: Path) -> None:
    _write_scope(
        vault,
        "pii",
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB3\nclasses: [\"pii\"]\n",
    )
    pol = policy.load(vault)
    _write_page(vault, "Knowledge Base/Notes/x.md", "type: insight\nclasses: [pii]")
    parsed = _parse(vault, "Knowledge Base/Notes/x.md")
    assert "01ARZ3NDEKTSV4RRFFQ69G5FB3" in membership.evaluate(parsed, pol)


def test_ref_selector_resolves_membership(vault: Path) -> None:
    _write_scope(
        vault,
        "explicit",
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB4\n"
        "refs: [\"Knowledge Base/Notes/x.md\"]\n",
    )
    pol = policy.load(vault)
    _write_page(vault, "Knowledge Base/Notes/x.md", "type: insight")
    parsed = _parse(vault, "Knowledge Base/Notes/x.md")
    assert "01ARZ3NDEKTSV4RRFFQ69G5FB4" in membership.evaluate(parsed, pol)


def test_exclude_selector_removes_membership(vault: Path) -> None:
    _write_scope(
        vault,
        "acmeco",
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        "paths: [\"Knowledge Base/Projects/AcmeCo/**\"]\n"
        "exclude:\n  tags: [\"public\"]\n",
    )
    pol = policy.load(vault)
    _write_page(
        vault,
        "Knowledge Base/Projects/AcmeCo/public-note.md",
        "type: source\ntags: [public]",
    )
    parsed = _parse(vault, "Knowledge Base/Projects/AcmeCo/public-note.md")
    assert membership.evaluate(parsed, pol) == frozenset()


def test_empty_policy_never_matches(vault: Path) -> None:
    _write_page(vault, "Knowledge Base/Notes/x.md", "type: insight")
    parsed = _parse(vault, "Knowledge Base/Notes/x.md")
    assert membership.evaluate(parsed, policy.EMPTY_POLICY) == frozenset()


def test_memo_invalidates_on_fingerprint_change(vault: Path) -> None:
    membership.clear_memo()
    scope_path = _write_scope(
        vault,
        "acmeco",
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nprojects: [\"acmeco\"]\n",
    )
    pol1 = policy.load(vault)
    _write_page(vault, "Knowledge Base/Notes/x.md", "type: insight\nproject: acmeco")
    parsed = _parse(vault, "Knowledge Base/Notes/x.md")
    assert "01ARZ3NDEKTSV4RRFFQ69G5FAV" in membership.evaluate(parsed, pol1)

    # Rewrite the scope so it no longer matches this page's project.
    old_mtime = scope_path.stat().st_mtime
    scope_path.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nprojects: [\"other-co\"]\n",
        encoding="utf-8",
    )
    os.utime(scope_path, (old_mtime, old_mtime))
    pol2 = policy.load(vault)
    assert pol2.fingerprint != pol1.fingerprint

    assert membership.evaluate(parsed, pol2) == frozenset()
    # The stale memo entry for pol1's fingerprint is untouched (still cached).
    assert "01ARZ3NDEKTSV4RRFFQ69G5FAV" in membership.evaluate(parsed, pol1)


def test_memo_invalidates_on_mtime_change(vault: Path) -> None:
    membership.clear_memo()
    _write_scope(
        vault,
        "sources",
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB2\ntypes: [\"source\"]\n",
    )
    pol = policy.load(vault)
    page_path = _write_page(vault, "Knowledge Base/Sources/x.md", "type: insight")
    parsed = _parse(vault, "Knowledge Base/Sources/x.md")
    assert membership.evaluate(parsed, pol) == frozenset()

    time.sleep(0.01)
    page_path.write_text("---\ntype: source\n---\nbody\n", encoding="utf-8")
    reparsed = _parse(vault, "Knowledge Base/Sources/x.md")
    assert "01ARZ3NDEKTSV4RRFFQ69G5FB2" in membership.evaluate(reparsed, pol)


# --------------------------------------------------------------------------
# Kernel residual — an unreadable page must not resolve to "member of nothing"
# --------------------------------------------------------------------------


def test_unstattable_page_refuses_to_resolve_membership(vault: Path) -> None:
    """`except OSError` fell back to `page.mtime`, which is fail-OPEN.

    The stat is the cache-validity probe AND the only evidence that the page
    on disk is the page that was parsed. When it fails, the honest answer is
    "unresolvable" — and it must not be spelled `frozenset()`, because an
    empty scope set means "member of no scope", which `decisions.decide`
    resolves to DISCLOSURE_MAX. Silently degrading to a stale mtime turned an
    unreadable file into full disclosure.
    """
    import pytest

    _write_scope(
        vault,
        "patterns",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: ["Knowledge Base/Notes/Patterns/**"]\n',
    )
    pol = policy.load(vault)
    page_path = _write_page(vault, "Knowledge Base/Notes/Patterns/p.md", "type: pattern")
    parsed = _parse(vault, "Knowledge Base/Notes/Patterns/p.md")
    membership.clear_memo()

    page_path.unlink()
    with pytest.raises(membership.MembershipUnresolved):
        membership.evaluate(parsed, pol)


def test_unresolvable_membership_makes_the_decision_fail_closed(
    vault: Path, monkeypatch
) -> None:
    """The caller contract: `_decide_path` must translate "unresolvable" into
    `None` — its established fail-closed signal — not into a level."""
    from exomem.governance import egress

    _write_scope(
        vault,
        "patterns",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: ["Knowledge Base/Notes/Patterns/**"]\n',
    )
    rules = vault / "Knowledge Base" / "_Governance" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "r.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 6\n',
        encoding="utf-8",
    )
    _write_page(vault, "Knowledge Base/Notes/Patterns/p.md", "type: pattern")
    pol = policy.load(vault)
    membership.clear_memo()
    egress.clear_decision_memo()

    def _boom(page, policy_arg, *, content_hash):
        assert len(content_hash) == 64
        assert all(char in "0123456789abcdef" for char in content_hash)
        raise membership.MembershipUnresolved("Knowledge Base/Notes/Patterns/p.md")

    monkeypatch.setattr(egress.membership_module, "evaluate_snapshot", _boom)
    decision = egress._decide_path(
        vault,
        "Knowledge Base/Notes/Patterns/p.md",
        policy=pol,
        audience="external",
        purpose=None,
        grants_hash="",
    )
    assert decision is None


@pytest.mark.parametrize(
    ("selector", "value"),
    [
        ("projects", "acmeco"),
        ("tags", "confidential"),
        ("types", "source"),
        ("classes", "pii"),
    ],
)
def test_missing_non_markdown_companion_is_unresolved_for_semantic_selectors(
    vault: Path, selector: str, value: str
) -> None:
    _write_scope(
        vault,
        selector,
        f"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n{selector}: [\"{value}\"]\n",
    )
    pol = policy.load(vault)
    asset = vault / "Knowledge Base/Notes/asset.bin"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"\\x00binary")

    outcome = membership.evaluate_path_only(vault, "Knowledge Base/Notes/asset.bin", pol)

    # A bare empty set means "not in any scope", which fail-opens through
    # decisions.decide.  Missing descriptor-like companions must instead be
    # explicit, non-iterable unresolved membership.
    assert type(outcome).__name__ == "MembershipOutcome"
    assert outcome.state == "unresolved"
    assert outcome.reason == "companion_required"


def test_path_only_non_markdown_membership_is_classified_empty(vault: Path) -> None:
    _write_scope(
        vault,
        "paths",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: ["Knowledge Base/Private/**"]\n',
    )
    pol = policy.load(vault)

    outcome = membership.evaluate_path_only(vault, "Knowledge Base/Notes/asset.bin", pol)

    assert type(outcome).__name__ == "MembershipOutcome"
    assert outcome.state == "classified"
    assert outcome.scope_ids == frozenset()


def test_path_exclusion_proves_non_markdown_scope_is_excluded(vault: Path) -> None:
    _write_scope(
        vault,
        "excluded",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntypes: ["source"]\nexclude:\n  paths: ["Knowledge Base/Notes/asset.bin"]\n',
    )
    pol = policy.load(vault)

    outcome = membership.evaluate_path_only(vault, "Knowledge Base/Notes/asset.bin", pol)

    assert type(outcome).__name__ == "MembershipOutcome"
    assert outcome.state == "classified"
    assert outcome.scope_ids == frozenset()


def test_path_match_does_not_erase_an_unresolved_semantic_sibling(vault: Path) -> None:
    _write_scope(
        vault,
        "path",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: ["Knowledge Base/Notes/asset.bin"]\n',
    )
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB1\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)

    outcome = membership.evaluate_path_only(vault, "Knowledge Base/Notes/asset.bin", pol)

    assert type(outcome).__name__ == "MembershipOutcome"
    assert outcome.state == "unresolved"
    assert outcome.scope_ids == frozenset({"01ARZ3NDEKTSV4RRFFQ69G5FAV"})
