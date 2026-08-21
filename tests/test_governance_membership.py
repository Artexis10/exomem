"""Query-time scope membership: each selector kind, excludes, memo invalidation.

Membership is evaluated against an already-parsed `ParsedPage` (design D4) —
never an index-time table — and memoized per `(fingerprint, path, mtime_ns)`.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest
import yaml

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
    assert outcome.reason == "descriptor_missing"


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


def _write_binary_companion(
    vault: Path,
    *,
    artifact_rel: str = "Knowledge Base/Notes/asset.bin",
    semantics: str = (
        "projects: [acmeco]\n"
        "    tags: [confidential]\n"
        "    types: [source]\n"
        "    classes: [pii]"
    ),
    artifact_sha256: str | None = None,
    artifact_size: int | None = None,
    state: str = "classified",
) -> Path:
    artifact = vault / artifact_rel
    raw = artifact.read_bytes()
    companion = artifact.with_name(f"{artifact.name}.md")
    companion.write_text(
        "---\n"
        "type: source\n"
        "title: Bound binary companion\n"
        "governance_companion:\n"
        "  version: 1\n"
        f"  state: {state}\n"
        "  artifact_class: binary\n"
        f"  artifact_path: {artifact_rel}\n"
        f"  artifact_sha256: {artifact_sha256 or hashlib.sha256(raw).hexdigest()}\n"
        f"  artifact_size: {len(raw) if artifact_size is None else artifact_size}\n"
        "  semantics:\n"
        f"    {semantics}\n"
        "---\n"
        "Companion page metadata is not artifact semantics.\n",
        encoding="utf-8",
    )
    return companion


def _write_descriptor_page(path: Path, descriptor: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "type": "source",
        "title": "Bound artifact companion",
        "governance_companion": descriptor,
    }
    path.write_text(
        f"---\n{yaml.safe_dump(document, sort_keys=False)}---\nBound companion.\n",
        encoding="utf-8",
    )
    return path


def _bound_descriptor(
    artifact_rel: str,
    data: bytes,
    *,
    artifact_class: str,
    **fields: object,
) -> dict[str, object]:
    return {
        "version": 1,
        "state": "classified",
        "artifact_class": artifact_class,
        "artifact_path": artifact_rel,
        "artifact_sha256": hashlib.sha256(data).hexdigest(),
        "artifact_size": len(data),
        **fields,
        "semantics": {
            "projects": [],
            "tags": ["confidential"],
            "types": ["source"],
            "classes": [],
        },
    }


def _write_four_semantic_scopes(vault: Path) -> policy.Policy:
    selectors = (
        ("projects", "acmeco", "01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        ("tags", "confidential", "01ARZ3NDEKTSV4RRFFQ69G5FAW"),
        ("types", "source", "01ARZ3NDEKTSV4RRFFQ69G5FAX"),
        ("classes", "pii", "01ARZ3NDEKTSV4RRFFQ69G5FAY"),
    )
    for selector, value, scope_id in selectors:
        _write_scope(
            vault,
            selector,
            "governance_version: 1\n"
            f"id: {scope_id}\n"
            f'{selector}: ["{value}"]\n',
        )
    return policy.load(vault)


def test_bound_binary_companion_classifies_all_explicit_semantics(vault: Path) -> None:
    pol = _write_four_semantic_scopes(vault)
    asset = vault / "Knowledge Base/Notes/asset.bin"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"opaque binary")
    _write_binary_companion(vault)

    outcome = membership.evaluate_path_only(
        vault, "Knowledge Base/Notes/asset.bin", pol
    )

    assert outcome.state == "classified"
    assert outcome.scope_ids == frozenset(
        {
            "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "01ARZ3NDEKTSV4RRFFQ69G5FAW",
            "01ARZ3NDEKTSV4RRFFQ69G5FAX",
            "01ARZ3NDEKTSV4RRFFQ69G5FAY",
        }
    )


def test_explicitly_empty_bound_companion_is_classified_empty(vault: Path) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["secret"]\n',
    )
    pol = policy.load(vault)
    asset = vault / "Knowledge Base/Notes/asset.bin"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"opaque binary")
    _write_binary_companion(
        vault,
        semantics="projects: []\n    tags: []\n    types: []\n    classes: []",
    )

    outcome = membership.evaluate_path_only(
        vault, "Knowledge Base/Notes/asset.bin", pol
    )

    assert outcome.state == "classified"
    assert outcome.scope_ids == frozenset()


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"artifact_sha256": "0" * 64}, "artifact_mismatch"),
        ({"artifact_size": 999}, "artifact_mismatch"),
        ({"state": "pending"}, "descriptor_invalid"),
    ],
)
def test_malformed_or_stale_binary_companion_is_unresolved(
    vault: Path, overrides: dict[str, object], expected_reason: str
) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    asset = vault / "Knowledge Base/Notes/asset.bin"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"opaque binary")
    _write_binary_companion(vault, **overrides)

    outcome = membership.evaluate_path_only(
        vault, "Knowledge Base/Notes/asset.bin", pol
    )

    assert outcome.state == "unresolved"
    assert outcome.reason == expected_reason


def test_binary_companion_for_a_different_artifact_path_is_unresolved(
    vault: Path,
) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    artifact_rel = "Knowledge Base/Notes/asset.bin"
    data = b"opaque binary"
    artifact = vault / artifact_rel
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(data)
    _write_descriptor_page(
        artifact.with_name("asset.bin.md"),
        _bound_descriptor(
            "Knowledge Base/Notes/other.bin",
            data,
            artifact_class="binary",
        ),
    )

    outcome = membership.evaluate_path_only(vault, artifact_rel, pol)

    assert outcome.state == "unresolved"
    assert outcome.reason == "artifact_mismatch"


def test_companion_page_metadata_is_not_silently_adopted(vault: Path) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    asset = vault / "Knowledge Base/Notes/asset.bin"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"opaque binary")
    companion = asset.with_name("asset.bin.md")
    companion.write_text(
        "---\ntype: source\ntags: [confidential]\n---\nLegacy page metadata only.\n",
        encoding="utf-8",
    )

    outcome = membership.evaluate_path_only(
        vault, "Knowledge Base/Notes/asset.bin", pol
    )

    assert outcome.state == "unresolved"
    assert outcome.reason == "descriptor_missing"


def test_symlinked_binary_companion_is_unresolved(vault: Path, tmp_path: Path) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    asset = vault / "Knowledge Base/Notes/asset.bin"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"opaque binary")
    external = tmp_path / "external.md"
    external.write_text("---\ntype: source\n---\noutside\n", encoding="utf-8")
    asset.with_name("asset.bin.md").symlink_to(external)

    outcome = membership.evaluate_path_only(
        vault, "Knowledge Base/Notes/asset.bin", pol
    )

    assert outcome.state == "unresolved"
    assert outcome.reason == "companion_unsafe"


def test_unreadable_binary_companion_is_unresolved(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    artifact_rel = "Knowledge Base/Notes/asset.bin"
    asset = vault / artifact_rel
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"opaque binary")
    _write_binary_companion(vault)
    read = membership.companions.reserved_paths.read_generic_bytes

    def _refuse_companion(root, value, **kwargs):
        if str(value).endswith(".bin.md"):
            raise membership.companions.reserved_paths.ReservedPathLeafError(
                "IO_REFUSED"
            )
        return read(root, value, **kwargs)

    monkeypatch.setattr(
        membership.companions.reserved_paths,
        "read_generic_bytes",
        _refuse_companion,
    )

    outcome = membership.evaluate_path_only(vault, artifact_rel, pol)

    assert outcome.state == "unresolved"
    assert outcome.reason == "companion_unsafe"


def test_artifact_drift_invalidates_a_previously_valid_companion(vault: Path) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    asset = vault / "Knowledge Base/Notes/asset.bin"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"opaque binary")
    _write_binary_companion(vault)
    membership.clear_memo()

    first = membership.evaluate_path_only(
        vault, "Knowledge Base/Notes/asset.bin", pol
    )
    asset.write_bytes(b"changed binary")
    second = membership.evaluate_path_only(
        vault, "Knowledge Base/Notes/asset.bin", pol
    )

    assert first.state == "classified"
    assert first.scope_ids == frozenset({"01ARZ3NDEKTSV4RRFFQ69G5FAV"})
    assert second.state == "unresolved"
    assert second.reason == "artifact_mismatch"


def test_bound_media_companion_requires_media_identity(vault: Path) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    artifact_rel = "Knowledge Base/Evidence/client/call.mp4"
    data = b"video bytes"
    artifact = vault / artifact_rel
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(data)
    _write_descriptor_page(
        artifact.with_name("call.mp4.md"),
        _bound_descriptor(
            artifact_rel,
            data,
            artifact_class="media",
            media_type="video",
            original_filename="call.mp4",
        ),
    )

    outcome = membership.evaluate_path_only(vault, artifact_rel, pol)

    assert outcome.state == "classified"
    assert outcome.scope_ids == frozenset({"01ARZ3NDEKTSV4RRFFQ69G5FAV"})


def test_media_descriptor_without_original_filename_is_unresolved(vault: Path) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    artifact_rel = "Knowledge Base/Evidence/client/call.mp4"
    data = b"video bytes"
    artifact = vault / artifact_rel
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(data)
    _write_descriptor_page(
        artifact.with_name("call.mp4.md"),
        _bound_descriptor(
            artifact_rel,
            data,
            artifact_class="media",
            media_type="video",
        ),
    )

    outcome = membership.evaluate_path_only(vault, artifact_rel, pol)

    assert outcome.state == "unresolved"
    assert outcome.reason == "descriptor_invalid"


def test_unique_bound_dataset_card_classifies_data_file(vault: Path) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    artifact_rel = "Knowledge Base/Data/ledger.csv"
    data = b"amount\n42\n"
    artifact = vault / artifact_rel
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(data)
    descriptor = _bound_descriptor(
        artifact_rel,
        data,
        artifact_class="dataset",
        format="csv",
    )
    card = _write_descriptor_page(
        vault / "Knowledge Base/Notes/ledger-dataset.md", descriptor
    )
    card_text = card.read_text(encoding="utf-8").replace(
        "type: source\n", f"type: dataset\ndata_file: {artifact_rel}\nformat: csv\n", 1
    )
    card.write_text(card_text, encoding="utf-8")

    outcome = membership.evaluate_path_only(vault, artifact_rel, pol)

    assert outcome.state == "classified"
    assert outcome.scope_ids == frozenset({"01ARZ3NDEKTSV4RRFFQ69G5FAV"})


def test_duplicate_bound_dataset_cards_are_unresolved(vault: Path) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    artifact_rel = "Knowledge Base/Data/ledger.csv"
    data = b"amount\n42\n"
    artifact = vault / artifact_rel
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(data)
    descriptor = _bound_descriptor(
        artifact_rel,
        data,
        artifact_class="dataset",
        format="csv",
    )
    for name in ("ledger-a.md", "ledger-b.md"):
        card = _write_descriptor_page(vault / "Knowledge Base/Notes" / name, descriptor)
        card.write_text(
            card.read_text(encoding="utf-8").replace(
                "type: source\n",
                f"type: dataset\ndata_file: {artifact_rel}\nformat: csv\n",
                1,
            ),
            encoding="utf-8",
        )

    outcome = membership.evaluate_path_only(vault, artifact_rel, pol)

    assert outcome.state == "unresolved"
    assert outcome.reason == "companion_ambiguous"


def test_relocated_dataset_card_does_not_classify_the_new_artifact_path(
    vault: Path,
) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    artifact_rel = "Knowledge Base/Data/moved.csv"
    old_rel = "Knowledge Base/Data/original.csv"
    data = b"amount\n42\n"
    artifact = vault / artifact_rel
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(data)
    descriptor = _bound_descriptor(
        artifact_rel,
        data,
        artifact_class="dataset",
        format="csv",
    )
    card = _write_descriptor_page(
        vault / "Knowledge Base/Notes/ledger-dataset.md", descriptor
    )
    card.write_text(
        card.read_text(encoding="utf-8").replace(
            "type: source\n",
            f"type: dataset\ndata_file: {old_rel}\nformat: csv\n",
            1,
        ),
        encoding="utf-8",
    )

    outcome = membership.evaluate_path_only(vault, artifact_rel, pol)

    assert outcome.state == "unresolved"
    assert outcome.reason == "descriptor_missing"


@pytest.mark.parametrize(
    ("override", "expected_reason"),
    [
        ({"parent_sha256": "0" * 64}, "artifact_mismatch"),
        ({"parent_path": "Knowledge Base/Evidence/client/other.mp4"}, "artifact_mismatch"),
        ({"frame_timestamp_ms": 10001}, "artifact_mismatch"),
    ],
)
def test_scene_frame_companion_binds_parent_and_timestamp(
    vault: Path, override: dict[str, object], expected_reason: str
) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    parent_rel = "Knowledge Base/Evidence/client/call.mp4"
    parent_data = b"video bytes"
    parent = vault / parent_rel
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_bytes(parent_data)
    frame_rel = f"{parent_rel}.frames/scene-001-t10000ms.jpg"
    frame_data = b"frame bytes"
    frame = vault / frame_rel
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(frame_data)
    descriptor = _bound_descriptor(
        frame_rel,
        frame_data,
        artifact_class="scene_frame",
        parent_path=parent_rel,
        parent_sha256=hashlib.sha256(parent_data).hexdigest(),
        frame_timestamp_ms=10000,
    )
    descriptor.update(override)
    _write_descriptor_page(frame.with_name(f"{frame.name}.md"), descriptor)

    outcome = membership.evaluate_path_only(vault, frame_rel, pol)

    assert outcome.state == "unresolved"
    assert outcome.reason == expected_reason


def test_valid_scene_frame_companion_classifies(vault: Path) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["confidential"]\n',
    )
    pol = policy.load(vault)
    parent_rel = "Knowledge Base/Evidence/client/call.mp4"
    parent_data = b"video bytes"
    parent = vault / parent_rel
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_bytes(parent_data)
    frame_rel = f"{parent_rel}.frames/scene-001-t10000ms.jpg"
    frame_data = b"frame bytes"
    frame = vault / frame_rel
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(frame_data)
    _write_descriptor_page(
        frame.with_name(f"{frame.name}.md"),
        _bound_descriptor(
            frame_rel,
            frame_data,
            artifact_class="scene_frame",
            parent_path=parent_rel,
            parent_sha256=hashlib.sha256(parent_data).hexdigest(),
            frame_timestamp_ms=10000,
        ),
    )

    outcome = membership.evaluate_path_only(vault, frame_rel, pol)

    assert outcome.state == "classified"
    assert outcome.scope_ids == frozenset({"01ARZ3NDEKTSV4RRFFQ69G5FAV"})


def test_non_markdown_membership_memo_reuses_only_the_exact_bound_snapshots(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_scope(
        vault,
        "semantic",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["alpha"]\n',
    )
    pol = policy.load(vault)
    asset = vault / "Knowledge Base/Notes/asset.bin"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"opaque binary")
    _write_binary_companion(
        vault,
        semantics="projects: []\n    tags: [alpha]\n    types: []\n    classes: []",
    )
    membership.clear_memo()
    original = membership._semantic_scope_matches
    calls = 0

    def _counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(membership, "_semantic_scope_matches", _counted)
    first = membership.evaluate_path_only(
        vault, "Knowledge Base/Notes/asset.bin", pol
    )
    second = membership.evaluate_path_only(
        vault, "Knowledge Base/Notes/asset.bin", pol
    )

    assert first == second
    assert calls == 1


def test_non_markdown_membership_memo_never_replays_changed_companion_bytes(
    vault: Path,
) -> None:
    _write_scope(
        vault,
        "alpha",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: ["alpha"]\n',
    )
    _write_scope(
        vault,
        "bravo",
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAW\ntags: ["bravo"]\n',
    )
    pol = policy.load(vault)
    asset = vault / "Knowledge Base/Notes/asset.bin"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"opaque binary")
    companion = _write_binary_companion(
        vault,
        semantics="projects: []\n    tags: [alpha]\n    types: []\n    classes: []",
    )
    membership.clear_memo()

    first = membership.evaluate_path_only(
        vault, "Knowledge Base/Notes/asset.bin", pol
    )
    prior = companion.stat()
    changed = companion.read_text(encoding="utf-8").replace(
        "tags: [alpha]", "tags: [bravo]"
    )
    companion.write_text(changed, encoding="utf-8")
    os.utime(companion, ns=(prior.st_atime_ns, prior.st_mtime_ns))
    second = membership.evaluate_path_only(
        vault, "Knowledge Base/Notes/asset.bin", pol
    )

    assert first.scope_ids == frozenset({"01ARZ3NDEKTSV4RRFFQ69G5FAV"})
    assert second.scope_ids == frozenset({"01ARZ3NDEKTSV4RRFFQ69G5FAW"})
