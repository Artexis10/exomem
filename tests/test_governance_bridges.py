"""Cross-domain bridge governance: exact releases, stripping, and review."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote

import pytest

from exomem import attention, commands, review_state
from exomem import find as find_module
from exomem.governance import bridges, decisions, egress, membership, policy
from exomem.governance.principal import (
    RequestPrincipal,
    owner_principal,
    request_scope,
)

SCOPE_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
SCOPE_B = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
RELEASE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB1"
BRIDGE_REF = "exomem://memory/00000000-0000-4000-8000-000000000201"
SOURCE_REF = "exomem://memory/00000000-0000-4000-8000-000000000202"
BRIDGE_PATH = "Knowledge Base/Notes/Insights/workload-constraint.md"
SOURCE_PATH = "Knowledge Base/Notes/Research/Health/private-source.md"
SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture(autouse=True)
def _clear_policy_caches():
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()
    yield
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()


def _write_policy(vault: Path, kind: str, name: str, content: str) -> Path:
    target = vault / "Knowledge Base" / "_Governance" / kind / f"{name}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _scope_document(
    scope_id: str, *, constraint: str | None = None, path: str = SOURCE_PATH
) -> str:
    extra = f"constraint: {constraint!r}\n" if constraint is not None else ""
    return (
        "governance_version: 1\n"
        f"id: {scope_id}\n"
        f'paths: ["{path}"]\n'
        f"{extra}"
    )


def _rule_document(
    *,
    scope_ids: tuple[str, ...],
    ceiling: int = 2,
    extra: str = "",
    rule_id: str = RULE_ID,
    audience: str = "external",
) -> str:
    scopes = ", ".join(f'"{item}"' for item in scope_ids)
    return (
        "governance_version: 1\n"
        f"id: {rule_id}\n"
        f"scope_ids: [{scopes}]\n"
        f"audience: {audience}\n"
        f"ceiling: {ceiling}\n"
        f"{extra}"
    )


def _standing_grant_document(
    *, scope_ids: tuple[str, ...], ceiling: int = 6, audience: str = "external"
) -> str:
    scopes = ", ".join(f'"{item}"' for item in scope_ids)
    return (
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB9\n"
        "kind: standing\n"
        f"scope_ids: [{scopes}]\n"
        f"audience: {audience}\n"
        f"ceiling: {ceiling}\n"
    )


def _release_document(*, unknown: str = "") -> str:
    return (
        "governance_version: 1\n"
        f"id: {RELEASE_ID}\n"
        "kind: release\n"
        f"path: {BRIDGE_PATH!r}\n"
        f"ref: {BRIDGE_REF!r}\n"
        f"content_hash: {SHA_A}\n"
        "to_audience: external\n"
        "released_at: '2026-07-28T12:00:00Z'\n"
        "why: Owner reviewed the exact bridge draft\n"
        "bridge_scope: workload-planning\n"
        "bridge_of:\n"
        f"  - ref: {SOURCE_REF!r}\n"
        f"    path: {SOURCE_PATH!r}\n"
        f"    content_hash: {SHA_B}\n"
        "    restriction_signature: 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'\n"
        "options:\n"
        "  strip_provenance:\n"
        f"    - {SOURCE_REF!r}\n"
        f"{unknown}"
    )


def _external(audience: str = "external") -> RequestPrincipal:
    return RequestPrincipal(audience_id=audience, surface="mcp")


def _bridge_source_text(
    *, marker: str = "RESTRICTED-SENTINEL", title: str = "Private source title"
) -> str:
    return (
        "---\n"
        "type: research-note\n"
        "exomem_id: 00000000-0000-4000-8000-000000000202\n"
        f"title: {title}\n"
        "project: health\n"
        "status: active\n"
        "created: 2026-07-28\n"
        "updated: 2026-07-28\n"
        "sources: []\n"
        "tags: [private]\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{marker}\n"
    )


def _bridge_text(
    *,
    bridge_id: str = "00000000-0000-4000-8000-000000000201",
    review: str = "2026-12-01",
) -> str:
    return (
        "---\n"
        "type: insight\n"
        f"exomem_id: {bridge_id}\n"
        "title: Workload constraint\n"
        "status: active\n"
        "created: 2026-07-28\n"
        "updated: 2026-07-28\n"
        "sources:\n"
        f"  - '[[{SOURCE_PATH.removesuffix('.md')}]]'\n"
        f"bridge_of: [{SOURCE_REF!r}]\n"
        "bridge_scope: workload-planning\n"
        f"bridge_review: {review}\n"
        "tags: [planning]\n"
        "---\n\n"
        "# Workload constraint\n\n"
        "## Observations\n"
        "- [constraint] Do not assume unlimited evening capacity #planning ^capacity\n\n"
        "## Relations\n"
        f"- derived_from [[{SOURCE_PATH.removesuffix('.md')}]]\n"
    )


def _restriction_signature(vault: Path, *, audience: str = "external") -> str:
    return bridges.restriction_signature(
        {SCOPE_A}, policy=policy.load(vault), audience=audience
    )


def _write_bridge_fixture(
    vault: Path,
    *,
    approval: bool,
    review: str = "2026-12-01",
    source_title: str = "Private source title",
) -> tuple[Path, Path]:
    source = vault / SOURCE_PATH
    bridge = vault / BRIDGE_PATH
    source.parent.mkdir(parents=True, exist_ok=True)
    bridge.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(_bridge_source_text(title=source_title), encoding="utf-8")
    bridge.write_text(_bridge_text(review=review), encoding="utf-8")
    _write_policy(vault, "scopes", "private", _scope_document(SCOPE_A))
    _write_policy(vault, "rules", "private", _rule_document(scope_ids=(SCOPE_A,), ceiling=0))
    if approval:
        release = _release_document()
        release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
        release = release.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
        release = release.replace("c" * 64, _restriction_signature(vault), 1)
        _write_policy(vault, "grants", "bridge-release", release)
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()
    return bridge, source


def test_release_grant_is_a_strict_exact_item_type_not_a_standing_grant(
    vault: Path,
) -> None:
    _write_policy(vault, "grants", "bridge-release", _release_document())

    compiled = policy.load(vault)

    assert not compiled.blocked
    assert compiled.grants == ()
    assert len(compiled.release_grants) == 1
    release = compiled.release_grants[0]
    assert release.id == RELEASE_ID
    assert release.path == BRIDGE_PATH
    assert release.ref == BRIDGE_REF
    assert release.content_hash == SHA_A
    assert release.to_audience == "external"
    assert release.bridge_of[0].path == SOURCE_PATH
    assert release.bridge_of[0].ref == SOURCE_REF
    assert release.bridge_of[0].content_hash == SHA_B
    assert release.strip_provenance == (SOURCE_REF,)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace(f"ref: {BRIDGE_REF!r}\n", ""),
        lambda text: text.replace(f"content_hash: {SHA_A}\n", "content_hash: nope\n"),
        lambda text: text + "unexpected_release_field: true\n",
        lambda text: text.replace("kind: release", "kind: mystery"),
    ],
)
def test_release_grant_partial_unknown_or_malformed_fields_fail_closed(
    vault: Path, mutation,
) -> None:
    _write_policy(vault, "grants", "bridge-release", mutation(_release_document()))

    compiled = policy.load(vault)

    assert compiled.blocked
    assert compiled.release_grants == ()
    assert any(finding["severity"] == "error" for finding in compiled.findings)


@pytest.mark.parametrize("mutation", ["partial", "dependency-path", "dependency-ref"])
def test_raw_bridge_metadata_and_dependency_aliases_fail_closed(
    vault: Path, mutation: str
) -> None:
    bridge, _source = _write_bridge_fixture(vault, approval=True)
    if mutation == "partial":
        bridge.write_text(
            bridge.read_text(encoding="utf-8").replace("bridge_review: 2026-12-01\n", ""),
            encoding="utf-8",
        )
    else:
        release = _release_document()
        release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
        release = release.replace(SHA_B, hashlib.sha256((vault / SOURCE_PATH).read_bytes()).hexdigest(), 1)
        release = release.replace("c" * 64, _restriction_signature(vault), 1)
        if mutation == "dependency-path":
            release = release.replace(SOURCE_PATH, "../outside.md", 1)
        else:
            release = release.replace(SOURCE_REF, SOURCE_REF.upper(), 1)
        _write_policy(vault, "grants", "bridge-release", release)
        policy._CACHE.clear()

    with request_scope(_external()):
        assert egress.release_level_for(vault, BRIDGE_PATH) == 0


def test_bridge_release_cannot_widen_ordinary_ceiling_and_purpose_only_narrows(
    vault: Path,
) -> None:
    _write_bridge_fixture(vault, approval=True)
    _write_policy(vault, "scopes", "bridge", _scope_document(SCOPE_B, path=BRIDGE_PATH))
    _write_policy(
        vault,
        "rules",
        "bridge",
        _rule_document(
            scope_ids=(SCOPE_B,),
            ceiling=3,
            rule_id="01ARZ3NDEKTSV4RRFFQ69G5FBD",
        ),
    )
    _write_policy(
        vault,
        "rules",
        "bridge-purpose",
        _rule_document(
            scope_ids=(SCOPE_B,),
            ceiling=1,
            rule_id="01ARZ3NDEKTSV4RRFFQ69G5FBF",
            extra="purpose: audit\npurpose_condition: matches\n",
        ),
    )
    policy._CACHE.clear()

    with request_scope(_external()):
        assert egress.release_level_for(vault, BRIDGE_PATH) == 3
    compiled = policy.load(vault)
    assert decisions.decide((SCOPE_B,), audience="external", policy=compiled).level == 3
    assert decisions.decide((SCOPE_B,), audience="external", purpose="audit", policy=compiled).level == 1


def test_membership_only_dependency_drift_stales_bridge_release(vault: Path) -> None:
    _write_bridge_fixture(vault, approval=True)
    _write_policy(
        vault,
        "scopes",
        "also-private",
        (
            "governance_version: 1\n"
            f"id: {SCOPE_B}\n"
            "tags: [private]\n"
        ),
    )
    _write_policy(
        vault,
        "rules",
        "also-private",
        _rule_document(
            scope_ids=(SCOPE_B,), ceiling=0, rule_id="01ARZ3NDEKTSV4RRFFQ69G5FBE"
        ),
    )
    policy._CACHE.clear()

    with request_scope(_external()):
        assert egress.release_level_for(vault, BRIDGE_PATH) == 0


def test_scope_constraint_is_deterministic_and_never_uses_rule_option_order(
    vault: Path,
) -> None:
    _write_policy(
        vault,
        "scopes",
        "health",
        _scope_document(SCOPE_A, constraint="Do not assume unlimited evening capacity."),
    )
    _write_policy(vault, "rules", "health", _rule_document(scope_ids=(SCOPE_A,)))
    compiled = policy.load(vault)

    decision = decisions.decide((SCOPE_A,), audience="external", policy=compiled)

    assert decision.level == 2
    assert decision.options["constraint"] == "Do not assume unlimited evening capacity."
    assert decision.options["constraint_source"] == "scope"

    rendered = egress.project(
        {"path": SOURCE_PATH, "title": "must never serialize"},
        decision.level,
        decision=decision,
        scope_label="Private health material",
    )
    assert rendered == {
        "withheld": True,
        "level": 2,
        "constraint": "Do not assume unlimited evening capacity.",
    }


def test_legacy_rule_option_constraint_remains_the_scope_constraint_fallback(
    vault: Path,
) -> None:
    _write_policy(vault, "scopes", "health", _scope_document(SCOPE_A))
    _write_policy(
        vault,
        "rules",
        "health",
        _rule_document(
            scope_ids=(SCOPE_A,),
            extra="options:\n  constraint: Keep the legacy capacity boundary.\n",
        ),
    )

    decision = decisions.decide(
        (SCOPE_A,), audience="external", policy=policy.load(vault)
    )

    assert decision.level == 2
    assert decision.options["constraint"] == "Keep the legacy capacity boundary."
    assert "constraint_source" not in decision.options


def test_distinct_scope_constraints_fail_closed_instead_of_choosing_one(
    vault: Path,
) -> None:
    _write_policy(vault, "scopes", "a", _scope_document(SCOPE_A, constraint="Constraint A"))
    _write_policy(vault, "scopes", "b", _scope_document(SCOPE_B, constraint="Constraint B"))
    _write_policy(vault, "rules", "both", _rule_document(scope_ids=(SCOPE_A, SCOPE_B)))
    compiled = policy.load(vault)

    decision = decisions.decide((SCOPE_A, SCOPE_B), audience="external", policy=compiled)

    assert decision.level < 2
    assert "constraint" not in decision.options
    assert decision.options["constraint_ambiguous"] is True


@pytest.mark.parametrize(
    "constraint",
    [
        "[[Private source]]",
        "See Knowledge Base/Notes/private.md",
        "exomem://memory/00000000-0000-4000-8000-000000000202",
        "line one\nline two",
    ],
)
def test_scope_constraint_rejects_provenance_and_multiline_oracles(
    vault: Path, constraint: str,
) -> None:
    _write_policy(vault, "scopes", "health", _scope_document(SCOPE_A, constraint=constraint))

    compiled = policy.load(vault)

    assert compiled.blocked
    assert any(finding["code"] == "invalid_constraint" for finding in compiled.findings)


def test_complete_bridge_without_release_approval_is_withheld_everywhere(
    vault: Path,
) -> None:
    _write_bridge_fixture(vault, approval=False)

    with request_scope(_external()):
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=BRIDGE_PATH)
        hits = commands.op_find(
            vault,
            query="unlimited evening capacity",
            mode="keyword",
            graph=False,
            limit=10,
        )
        explained = commands.op_govern_memory(
            vault,
            operation="explain",
            path=BRIDGE_PATH,
            audience="external",
        )

    assert BRIDGE_PATH not in [hit.get("path") for hit in hits]
    assert explained["effective_ceiling"] == 0
    assert explained["release_reason"] == "RELEASE_UNAPPROVED"

    with request_scope(_external()):
        simulated = commands.op_govern_memory(
            vault,
            operation="simulate",
            paths=[BRIDGE_PATH],
            audience="external",
        )
    assert simulated["withheld_count"] == 1
    assert simulated["release_reasons"] == ["RELEASE_UNAPPROVED"]


def test_exact_approved_unchanged_bridge_releases_but_source_stays_withheld(
    vault: Path,
) -> None:
    _write_bridge_fixture(vault, approval=True)

    with request_scope(_external()):
        page = commands.op_get(vault, path=BRIDGE_PATH)
        hits = commands.op_find(
            vault,
            query="unlimited evening capacity",
            mode="keyword",
            graph=False,
            limit=10,
        )
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=SOURCE_PATH)

    assert "unlimited evening capacity" in page["body"]
    assert BRIDGE_PATH in [hit.get("path") for hit in hits]


def test_empty_policy_bridge_fast_path_never_parses_or_creates_governance_state(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args, **_kwargs):
        raise AssertionError("bridge machinery must not run on an empty-policy route")

    monkeypatch.setattr(bridges, "maybe_bridge", _boom)
    monkeypatch.setattr(bridges, "admit", _boom)
    monkeypatch.setattr(bridges, "strip_provenance", _boom)
    page = {"path": BRIDGE_PATH, "body": "ordinary bridge-shaped bytes", "frontmatter": {}}
    hits = [SimpleNamespace(path=BRIDGE_PATH)]
    payload = {"entries": [{"path": BRIDGE_PATH, "body": "ordinary bridge-shaped bytes"}]}
    original = json.dumps(payload, sort_keys=True)

    assert egress.annotate_page(vault, page, principal=_external()) == page
    assert egress.annotate_hits(vault, hits, principal=_external(), limit=10).hits == hits
    assert egress.filter_withheld_entries(vault, payload, principal=_external()) == payload
    assert json.dumps(payload, sort_keys=True) == original
    assert not (vault / "Knowledge Base" / "_Governance").exists()
    assert not list(vault.rglob("*.governance.sqlite"))


def test_same_size_timestamp_preserving_bridge_edit_is_release_stale(
    vault: Path,
) -> None:
    bridge, _source = _write_bridge_fixture(vault, approval=True)
    before = bridge.stat()
    original = bridge.read_text(encoding="utf-8")
    edited = original.replace("unlimited", "forbidden")
    assert len(edited.encode()) == len(original.encode())
    bridge.write_text(edited, encoding="utf-8")
    os.utime(bridge, ns=(before.st_atime_ns, before.st_mtime_ns))

    with request_scope(_external()):
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=BRIDGE_PATH)
        explained = commands.op_govern_memory(
            vault,
            operation="explain",
            path=BRIDGE_PATH,
            audience="external",
        )

    assert explained["release_reason"] == "RELEASE_STALE"


def test_retrieval_projection_swap_is_withheld_by_private_snapshot_identity(
    vault: Path,
) -> None:
    bridge, _source = _write_bridge_fixture(vault, approval=True)
    hits = find_module.find(
        vault,
        query="unlimited evening capacity",
        mode="keyword",
        graph=False,
        limit=10,
    )
    candidate = next(hit for hit in hits if hit.path == BRIDGE_PATH)
    assert candidate.snapshot_hash == hashlib.sha256(bridge.read_bytes()).hexdigest()
    before = bridge.stat()
    original = bridge.read_text(encoding="utf-8")
    edited = original.replace("unlimited", "forbidden")
    assert len(edited.encode()) == len(original.encode())
    bridge.write_text(edited, encoding="utf-8")
    os.utime(bridge, ns=(before.st_atime_ns, before.st_mtime_ns))

    annotated = egress.annotate_hits(
        vault,
        hits,
        principal=_external(),
        limit=10,
    )

    assert all(hit.path != BRIDGE_PATH for hit in annotated.hits)


def test_bridge_approval_does_not_transfer_to_copy_path_ref_or_audience(
    vault: Path,
) -> None:
    bridge, _source = _write_bridge_fixture(vault, approval=True)
    copy_path = "Knowledge Base/Notes/Insights/workload-constraint-copy.md"
    copied = _bridge_text(bridge_id="00000000-0000-4000-8000-000000000203")
    (vault / copy_path).write_text(copied, encoding="utf-8")

    with request_scope(_external()):
        assert commands.op_get(vault, path=BRIDGE_PATH)["path"] == BRIDGE_PATH
        assert any(
            hit.get("path") == BRIDGE_PATH
            for hit in commands.op_find(
                vault,
                query="unlimited evening capacity",
                mode="keyword",
                graph=False,
                limit=10,
            )
        )
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=copy_path)
    with request_scope(_external("other-audience")):
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=BRIDGE_PATH)

    assert bridge.is_file()


def test_duplicate_bridge_identity_stales_release_and_surfaces_bridge_review(
    vault: Path,
) -> None:
    bridge, _source = _write_bridge_fixture(vault, approval=True)
    duplicate = bridge.with_name("workload-constraint-duplicate.md")
    duplicate.write_bytes(bridge.read_bytes())

    with request_scope(_external()):
        assert egress.release_level_for(vault, BRIDGE_PATH) == 0

    report = attention.attention(vault, today=dt.date(2026, 7, 28), limit=0)
    item = next(item for item in report.items if "bridge_review" in item.categories)
    reason = next(reason for reason in item.reasons if reason["category"] == "bridge_review")
    assert reason["meta"]["cause"] == "BRIDGE_EDITED"


def test_filter_withheld_entries_recursively_strips_released_bridge_provenance(
    vault: Path,
) -> None:
    sentinel = "RESTRICTED-PROVENANCE-SENTINEL"
    bridge, source = _write_bridge_fixture(
        vault, approval=True, source_title=sentinel
    )
    source_path_alias = SOURCE_PATH.replace(" ", "%20").upper()
    source_ref_alias = SOURCE_REF.upper()
    payload = {
        "entries": [
            {
                "path": BRIDGE_PATH,
                "body": (
                    "Approved bridge prose remains.\n\n## Relations\n"
                    f"- derived_from [[{SOURCE_PATH.removesuffix('.md')}]]\n"
                ),
                "content": bridge.read_text(encoding="utf-8") + f"\n{sentinel}\n",
                "frontmatter": {
                    "bridge_of": [source_ref_alias],
                    "bridge_scope": "workload-planning",
                    "sources": [source_path_alias],
                    "evidence": [{"ref": source_ref_alias, "title": sentinel}],
                },
                "history": [
                    {
                        "nested": {
                            "excerpt": f"Reviewed {sentinel} at {source_path_alias}",
                        }
                    }
                ],
                "inbound": [{"source_ref": source_ref_alias, "source_title": sentinel}],
                "outbound": [f"[[{SOURCE_PATH.removesuffix('.md')}]]"],
                "graph": {
                    "seed": {
                        "node_key": "private-seed",
                        "excerpt": f"Seed mentions {sentinel} at {source_path_alias}",
                    },
                    "seeds": [{"node_key": "private", "ref": source_ref_alias, "title": sentinel}],
                    "nodes": [{"node_key": "private", "path": source_path_alias, "title": sentinel}],
                    "edges": [
                        {"src_key": "bridge", "dst_key": "private"},
                        {"src_key": "bridge", "dst_key": "private-seed"},
                    ],
                },
                "relation_match": {"target_path": source_path_alias, "title": sentinel},
                "superseded_by": {"parent_ref": source_ref_alias, "parent_title": sentinel},
                "supersedes": {"parent_path": source_path_alias, "parent_title": sentinel},
                "parent": {"path": source_path_alias, "title": sentinel},
                "provenance": {source_path_alias: "restricted mapping key"},
                "matched_units": [{"parent_ref": source_ref_alias, "parent_title": sentinel}],
            }
        ]
    }
    original = deepcopy(payload)
    bridge_bytes = bridge.read_bytes()
    source_bytes = source.read_bytes()

    with request_scope(_external()):
        filtered = egress.filter_withheld_entries(vault, payload)

    wire = json.dumps(filtered, sort_keys=True, default=str).casefold()
    for restricted in (SOURCE_REF, source_ref_alias, SOURCE_PATH, source_path_alias, sentinel):
        assert restricted.casefold() not in wire
    assert "bridge_scope" not in wire
    assert "private-seed" not in wire
    assert "approved bridge prose remains" in wire
    assert payload == original
    assert bridge.read_bytes() == bridge_bytes
    assert source.read_bytes() == source_bytes


def test_release_strips_dependency_provenance_from_direct_and_search_outputs(
    vault: Path,
) -> None:
    _write_bridge_fixture(vault, approval=True)

    with request_scope(_external()):
        page = commands.op_get(vault, path=BRIDGE_PATH)
        hits = commands.op_find(
            vault,
            query="unlimited evening capacity",
            mode="keyword",
            graph=True,
            limit=10,
        )

    page_wire = json.dumps(page, sort_keys=True, default=str).casefold()
    hits_wire = json.dumps(hits, sort_keys=True, default=str).casefold()
    for wire in (page_wire, hits_wire):
        assert SOURCE_REF.casefold() not in wire
        assert SOURCE_PATH.casefold() not in wire
        assert SOURCE_PATH.removesuffix(".md").casefold() not in wire
        assert "private source title" not in wire
        assert "bridge_scope" not in wire
    assert "content" not in page
    assert "bridge_of" not in page.get("frontmatter", {})
    assert "unlimited evening capacity" in page["body"]


def test_due_bridge_is_in_default_attention_with_date_stable_signal(
    vault: Path,
) -> None:
    _write_bridge_fixture(vault, approval=True)

    first = attention.attention(vault, today=dt.date(2026, 12, 1), limit=0)
    second = attention.attention(vault, today=dt.date(2026, 12, 9), limit=0)

    item = next(item for item in first.items if "bridge_review" in item.categories)
    later = next(candidate for candidate in second.items if candidate.ref == item.ref)
    reason = next(reason for reason in item.reasons if reason["category"] == "bridge_review")
    assert reason["meta"]["cause"] == "DUE_REVIEW"
    assert item.fingerprint == later.fingerprint
    wire = json.dumps(item.as_dict(), sort_keys=True).casefold()
    assert SOURCE_PATH.casefold() not in wire
    assert SOURCE_REF.casefold() not in wire
    assert "private source title" not in wire
    assert not review_state.state_path(vault).exists()


def test_due_bridge_review_context_is_recursively_provenance_stripped(
    vault: Path,
) -> None:
    _write_bridge_fixture(vault, approval=True, review="2026-01-01")
    item = next(
        item
        for item in attention.attention(vault, limit=0).items
        if "bridge_review" in item.categories
    )

    with request_scope(_external()):
        context = commands.op_review_item_context(
            vault,
            ref=item.ref,
            expected_fingerprint=item.fingerprint,
        )

    wire = json.dumps(context, sort_keys=True, default=str).casefold()
    assert "unlimited evening capacity" in wire
    assert SOURCE_PATH.casefold() not in wire
    assert SOURCE_REF.casefold() not in wire
    assert "private source title" not in wire


@pytest.mark.parametrize(
    ("mutation", "cause"),
    [
        ("bytes", "SOURCE_CHANGED_OR_RESTRICTION_CHANGED"),
        ("deleted", "SOURCE_UNAVAILABLE_OR_AMBIGUOUS"),
        ("restriction", "SOURCE_CHANGED_OR_RESTRICTION_CHANGED"),
    ],
)
def test_source_change_invalidates_release_and_resurfaces_dismissed_review(
    vault: Path, mutation: str, cause: str
) -> None:
    _bridge, source = _write_bridge_fixture(vault, approval=True)
    due = next(
        item
        for item in attention.attention(
            vault, today=dt.date(2026, 12, 1), limit=0
        ).items
        if "bridge_review" in item.categories
    )
    with request_scope(_external()):
        assert commands.op_get(vault, path=BRIDGE_PATH)["path"] == BRIDGE_PATH
    review_state.ReviewStateStore(vault).apply(
        due.item_id,
        due.fingerprint,
        action="dismiss",
        why="reviewed",
    )
    if mutation == "bytes":
        source.write_text(_bridge_source_text(marker="CHANGED-SENTINEL"), encoding="utf-8")
    elif mutation == "deleted":
        source.unlink()
    else:
        _write_policy(
            vault,
            "rules",
            "private",
            _rule_document(scope_ids=(SCOPE_A,), ceiling=1),
        )
        policy._CACHE.clear()
        policy._LAST_GOOD.clear()

    report = attention.attention(vault, today=dt.date(2026, 12, 2), limit=0)

    item = next(item for item in report.items if "bridge_review" in item.categories)
    reason = next(reason for reason in item.reasons if reason["category"] == "bridge_review")
    assert reason["meta"]["cause"] == cause
    assert item.fingerprint != due.fingerprint
    assert item.state == "open"
    with request_scope(_external()):
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=BRIDGE_PATH)
        assert all(
            hit.get("path") != BRIDGE_PATH
            for hit in commands.op_find(
                vault,
                query="unlimited evening capacity",
                mode="keyword",
                graph=False,
                limit=10,
            )
        )


def test_dependency_change_cannot_reuse_hot_bridge_decision(vault: Path) -> None:
    _bridge, source = _write_bridge_fixture(vault, approval=True)
    compiled = policy.load(vault)
    first = egress._decide_path(
        vault,
        BRIDGE_PATH,
        policy=compiled,
        audience="external",
        purpose=None,
        grants_hash=egress._grants_hash(compiled),
    )
    assert first is not None and first.level >= egress.RELEASE_FLOOR
    source.write_text(_bridge_source_text(marker="CHANGED-SENTINEL"), encoding="utf-8")

    second = egress._decide_path(
        vault,
        BRIDGE_PATH,
        policy=compiled,
        audience="external",
        purpose=None,
        grants_hash=egress._grants_hash(compiled),
    )

    assert second is not None and second.level == 0
    assert second.release_reason == "RELEASE_STALE"


@pytest.mark.parametrize(
    "mutation",
    ["bytes", "path", "ref", "deleted", "ambiguous", "restriction"],
)
def test_every_dependency_identity_or_restriction_drift_is_immediately_stale(
    vault: Path,
    mutation: str,
) -> None:
    _bridge, source = _write_bridge_fixture(vault, approval=True)
    with request_scope(_external()):
        assert egress.release_level_for(vault, BRIDGE_PATH) == 6

    if mutation == "bytes":
        source.write_text(_bridge_source_text(marker="changed"), encoding="utf-8")
    elif mutation == "path":
        moved = source.with_name("private-source-moved.md")
        source.rename(moved)
    elif mutation == "ref":
        source.write_text(
            _bridge_source_text().replace(
                "00000000-0000-4000-8000-000000000202",
                "00000000-0000-4000-8000-000000000299",
            ),
            encoding="utf-8",
        )
    elif mutation == "deleted":
        source.unlink()
    elif mutation == "ambiguous":
        duplicate = source.with_name("private-source-duplicate.md")
        duplicate.write_bytes(source.read_bytes())
    else:
        _write_policy(
            vault,
            "rules",
            "private",
            _rule_document(scope_ids=(SCOPE_A,), ceiling=1),
        )
        policy._CACHE.clear()
        policy._LAST_GOOD.clear()

    with request_scope(_external()):
        assert egress.release_level_for(vault, BRIDGE_PATH) == 0
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=BRIDGE_PATH)


@pytest.mark.parametrize(
    "rule_extra",
    [
        "purpose: planning\n",
        "purpose: planning\npurpose_condition: outside\n",
        "options:\n  suspended: true\n",
    ],
    ids=["purpose", "purpose-condition", "suspended"],
)
def test_relevant_rule_semantics_stale_bridge_release(
    vault: Path, rule_extra: str
) -> None:
    _write_bridge_fixture(vault, approval=True)
    _write_policy(
        vault,
        "rules",
        "private",
        _rule_document(scope_ids=(SCOPE_A,), ceiling=0, extra=rule_extra),
    )
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()

    with request_scope(_external()):
        assert egress.release_level_for(vault, BRIDGE_PATH) == 0


@pytest.mark.parametrize("change", ["standing-grant", "scope-constraint"])
def test_relevant_grant_and_constraint_stale_bridge_release(
    vault: Path, change: str
) -> None:
    _write_bridge_fixture(vault, approval=True)
    if change == "standing-grant":
        _write_policy(
            vault,
            "grants",
            "standing",
            _standing_grant_document(scope_ids=(SCOPE_A,)),
        )
    else:
        _write_policy(
            vault,
            "scopes",
            "private",
            _scope_document(SCOPE_A, constraint="Use the reviewed capacity limit."),
        )
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()

    with request_scope(_external()):
        assert egress.release_level_for(vault, BRIDGE_PATH) == 0


def test_unrelated_restriction_inputs_do_not_stale_bridge_release(vault: Path) -> None:
    _write_bridge_fixture(vault, approval=True)
    unrelated_scope = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
    _write_policy(
        vault,
        "scopes",
        "unrelated",
        _scope_document(
            unrelated_scope,
            constraint="Unrelated constraint.",
            path="Knowledge Base/Notes/Patterns/unrelated.md",
        ),
    )
    _write_policy(
        vault,
        "rules",
        "unrelated-external",
        _rule_document(
            scope_ids=(unrelated_scope,),
            ceiling=0,
            rule_id="01ARZ3NDEKTSV4RRFFQ69G5FBB",
        ),
    )
    _write_policy(
        vault,
        "rules",
        "same-scope-other-audience",
        (
            "governance_version: 1\n"
            "id: 01ARZ3NDEKTSV4RRFFQ69G5FBC\n"
            f'scope_ids: ["{SCOPE_A}"]\n'
            "audience: partner\n"
            "ceiling: 0\n"
        ),
    )
    _write_policy(
        vault,
        "grants",
        "unrelated-standing",
        _standing_grant_document(scope_ids=(unrelated_scope,)),
    )
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()

    with request_scope(_external()):
        assert commands.op_get(vault, path=BRIDGE_PATH)["path"] == BRIDGE_PATH


def test_restriction_signature_preserves_typed_nested_option_keys() -> None:
    common = {
        "fingerprint": "test",
        "rules": (
            policy.Rule(
                id=RULE_ID,
                source="rules/private.yaml",
                scope_ids=(SCOPE_A,),
                audience="external",
                ceiling=0,
            ),
        ),
    }
    integer_key = policy.Policy(
        **{
            **common,
            "rules": (
                policy.Rule(
                    id=RULE_ID,
                    source="rules/private.yaml",
                    scope_ids=(SCOPE_A,),
                    audience="external",
                    ceiling=0,
                    options={"nested": {1: "value"}},
                ),
            ),
        }
    )
    string_key = policy.Policy(
        **{
            **common,
            "rules": (
                policy.Rule(
                    id=RULE_ID,
                    source="rules/private.yaml",
                    scope_ids=(SCOPE_A,),
                    audience="external",
                    ceiling=0,
                    options={"nested": {"1": "value"}},
                ),
            ),
        }
    )

    assert bridges.restriction_signature(
        {SCOPE_A}, policy=integer_key, audience="external"
    ) != bridges.restriction_signature(
        {SCOPE_A}, policy=string_key, audience="external"
    )


def test_restriction_signature_preserves_container_types() -> None:
    def signature(options: dict) -> str:
        compiled = policy.Policy(
            fingerprint="test",
            rules=(
                policy.Rule(
                    id=RULE_ID,
                    source="rules/private.yaml",
                    scope_ids=(SCOPE_A,),
                    audience="external",
                    ceiling=0,
                    options=options,
                ),
            ),
        )
        return bridges.restriction_signature(
            {SCOPE_A}, policy=compiled, audience="external"
        )

    assert signature({"value": ["a", "b"]}) != signature({"value": ("a", "b")})
    assert signature({"value": ["a", "b"]}) != signature({"value": {"a", "b"}})
    assert signature({"value": {"a", "b"}}) != signature({"value": frozenset({"a", "b"})})
    assert signature({"value": b"binary"}) != signature({"value": "b'binary'"})


def test_safe_loaded_rule_options_stale_without_get_find_or_attention_crash(
    vault: Path,
) -> None:
    _write_bridge_fixture(vault, approval=True)
    _write_policy(
        vault,
        "rules",
        "private",
        _rule_document(
            scope_ids=(SCOPE_A,),
            ceiling=0,
            extra=(
                "options:\n"
                "  binary: !!binary AQI=\n"
                "  day: 2026-07-28\n"
                "  instant: 2026-07-28T12:00:00Z\n"
                "  members: !!set {a: null, b: null}\n"
                "  not_a_number: .nan\n"
                "  infinity: .inf\n"
            ),
        ),
    )
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()

    compiled = policy.load(vault)
    values = compiled.rules[0].options
    assert values["binary"] == b"\x01\x02"
    assert isinstance(values["day"], dt.date)
    assert isinstance(values["instant"], dt.datetime)
    assert values["members"] == {"a", "b"}

    with request_scope(_external()):
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=BRIDGE_PATH)
        assert all(
            hit.get("path") != BRIDGE_PATH
            for hit in commands.op_find(
                vault,
                query="unlimited evening capacity",
                mode="keyword",
                graph=False,
                limit=10,
            )
        )
    item = next(
        item
        for item in attention.attention(vault, today=dt.date(2026, 7, 28), limit=0).items
        if "bridge_review" in item.categories
    )
    reason = next(reason for reason in item.reasons if reason["category"] == "bridge_review")
    assert reason["meta"]["cause"] == "SOURCE_CHANGED_OR_RESTRICTION_CHANGED"


def test_unsupported_rule_option_fails_closed_without_crashing_admission(
    vault: Path,
) -> None:
    bridge, _source = _write_bridge_fixture(vault, approval=True)
    compiled = policy.load(vault)
    unsupported = replace(
        compiled,
        rules=(replace(compiled.rules[0], options={"unsupported": object()}),),
    )

    with pytest.raises(ValueError, match="unsupported option value"):
        bridges.restriction_signature({SCOPE_A}, policy=unsupported, audience="external")
    admission = bridges.admit(
        vault,
        BRIDGE_PATH,
        bridge.read_bytes(),
        policy=unsupported,
        audience="external",
    )
    assert admission.allowed is False
    assert admission.reason == "RELEASE_STALE"


def test_unrelated_policy_change_does_not_stale_and_exact_reapproval_restores(
    vault: Path,
) -> None:
    bridge, source = _write_bridge_fixture(vault, approval=True)
    unrelated_scope = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
    unrelated_rule = "01ARZ3NDEKTSV4RRFFQ69G5FB3"
    _write_policy(
        vault,
        "scopes",
        "unrelated",
        (
            "governance_version: 1\n"
            f"id: {unrelated_scope}\n"
            'paths: ["Knowledge Base/Notes/Patterns/unrelated.md"]\n'
        ),
    )
    _write_policy(
        vault,
        "rules",
        "unrelated",
        (
            "governance_version: 1\n"
            f"id: {unrelated_rule}\n"
            f'scope_ids: ["{unrelated_scope}"]\n'
            "audience: external\n"
            "ceiling: 0\n"
        ),
    )
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    egress.clear_decision_memo()
    with request_scope(_external()):
        assert commands.op_get(vault, path=BRIDGE_PATH)["path"] == BRIDGE_PATH

    source.write_text(_bridge_source_text(marker="current-reviewed-source"), encoding="utf-8")
    with request_scope(_external()):
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=BRIDGE_PATH)

    renewed = _release_document()
    renewed = renewed.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    renewed = renewed.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    renewed = renewed.replace("c" * 64, _restriction_signature(vault), 1)
    _write_policy(vault, "grants", "bridge-release", renewed)
    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    egress.clear_decision_memo()

    with request_scope(_external()):
        assert commands.op_get(vault, path=BRIDGE_PATH)["path"] == BRIDGE_PATH


def test_bridge_review_state_is_independent_per_approved_audience(vault: Path) -> None:
    bridge, source = _write_bridge_fixture(vault, approval=True)
    second_id = "01ARZ3NDEKTSV4RRFFQ69G5FB2"
    second = _release_document().replace(RELEASE_ID, second_id, 1)
    second = second.replace("to_audience: external", "to_audience: partner", 1)
    second = second.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    second = second.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    second = second.replace("c" * 64, _restriction_signature(vault, audience="partner"), 1)
    _write_policy(vault, "grants", "bridge-release-partner", second)
    policy._CACHE.clear()

    report = attention.attention(vault, today=dt.date(2026, 12, 1), limit=0)
    bridge_items = [item for item in report.items if "bridge_review" in item.categories]

    assert len(bridge_items) == 2
    assert len({item.item_id for item in bridge_items}) == 2
    dismissed = bridge_items[0]
    review_state.ReviewStateStore(vault).apply(
        dismissed.item_id,
        dismissed.fingerprint,
        action="dismiss",
    )
    remaining = attention.attention(vault, today=dt.date(2026, 12, 1), limit=0)
    visible = [item for item in remaining.items if "bridge_review" in item.categories]
    assert len(visible) == 1
    assert visible[0].item_id != dismissed.item_id


def test_normal_remember_review_flow_normalizes_and_binds_bridge_fields(
    vault: Path,
) -> None:
    source = vault / SOURCE_PATH
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(_bridge_source_text(), encoding="utf-8")
    _write_policy(vault, "scopes", "private", _scope_document(SCOPE_A))
    _write_policy(vault, "rules", "private", _rule_document(scope_ids=(SCOPE_A,), ceiling=0))
    kwargs = {
        "content": "## Claim\n\nDo not assume unlimited evening capacity.\n",
        "title": "Authored workload bridge",
        "slug": "authored-workload-bridge",
        "suggestions": False,
        "bridge_of": [SOURCE_PATH],
        "bridge_scope": "workload-planning",
        "bridge_review": "2026-12-01",
    }

    draft = commands.op_remember(vault, validate_only=True, **kwargs)
    changed = commands.op_remember(
        vault,
        validate_only=True,
        draft_id=draft["draft_id"],
        draft_token=draft["draft_token"],
        **{**kwargs, "bridge_scope": "capacity-planning"},
    )

    assert draft["draft_hash"] != changed["draft_hash"]
    with pytest.raises(ValueError, match="DRAFT_HASH_MISMATCH"):
        commands.op_remember(
            vault,
            draft_id=draft["draft_id"],
            draft_hash=draft["draft_hash"],
            draft_token=draft["draft_token"],
            relation_disposition="reviewed_none",
            relation_review_hash=draft["draft_hash"],
            relation_review_reason="The old bridge metadata review cannot authorize this draft.",
            **{**kwargs, "bridge_scope": "capacity-planning"},
        )
    result = commands.op_remember(
        vault,
        draft_id=draft["draft_id"],
        draft_hash=draft["draft_hash"],
        draft_token=draft["draft_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=draft["draft_hash"],
        relation_review_reason="Bridge metadata records provenance; no body edge is needed.",
        **kwargs,
    )
    raw = (vault / result["path"]).read_text(encoding="utf-8")

    assert SOURCE_REF in raw
    assert f"bridge_scope: {kwargs['bridge_scope']}" in raw
    assert f"bridge_review: {kwargs['bridge_review']}" in raw
    assert SOURCE_PATH not in raw
    assert policy.load(vault).release_grants == ()
    with request_scope(_external()):
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=result["path"])


@pytest.mark.parametrize(
    "fields",
    [
        {"bridge_of": [SOURCE_PATH]},
        {"bridge_scope": "workload-planning", "bridge_review": "2026-12-01"},
        {
            "bridge_of": [SOURCE_PATH],
            "bridge_scope": "not valid",
            "bridge_review": "2026-12-01",
        },
    ],
)
def test_normal_authoring_rejects_partial_or_malformed_bridge_fields(
    vault: Path,
    fields: dict,
) -> None:
    source = vault / SOURCE_PATH
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(_bridge_source_text(), encoding="utf-8")

    with pytest.raises(ValueError, match="INVALID_NOTE"):
        commands.op_remember(
            vault,
            content="A bridge draft.",
            title="Invalid bridge",
            slug="invalid-bridge",
            suggestions=False,
            validate_only=True,
            **fields,
        )


def test_release_approval_uses_reviewed_policy_commit_and_durable_causation(
    vault: Path,
) -> None:
    from exomem.governance import receipts
    from exomem.governance.tool import op_govern_memory

    bridge, source = _write_bridge_fixture(vault, approval=False)
    release = _release_document()
    release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    release = release.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    release = release.replace("c" * 64, _restriction_signature(vault), 1)
    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Release this exact reviewed bridge draft to the external audience",
        documents={"grants/bridge-release.yaml": release},
        selector_paths=[BRIDGE_PATH],
        target_ceiling=6,
        duration="standing",
    )

    committed = op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=proposal["proposal_id"],
    )

    compiled = policy.load(vault)
    assert compiled.release_grants[0].id == RELEASE_ID
    related = [
        row
        for row in receipts.event_records(vault)
        if row.get("event_id") == committed["event_id"]
        or row.get("causation_id") == committed["event_id"]
    ]
    assert any(row.get("phase") == "intent" for row in related)
    assert any(row.get("phase") == "committed" for row in related)


@pytest.mark.parametrize("path", ["../outside.md", "/tmp/outside.md"])
def test_bridge_release_rejects_outside_bridge_or_dependency_paths_without_an_oracle(
    vault: Path, path: str
) -> None:
    bridge, _source = _write_bridge_fixture(vault, approval=True)
    release = _release_document()
    release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    release = release.replace(SHA_B, hashlib.sha256((vault / SOURCE_PATH).read_bytes()).hexdigest(), 1)
    release = release.replace("c" * 64, _restriction_signature(vault), 1)
    release = release.replace(BRIDGE_PATH, path, 1)
    _write_policy(vault, "grants", "bridge-release", release)
    policy._CACHE.clear()

    with request_scope(_external()):
        assert egress.release_level_for(vault, BRIDGE_PATH) == 0


def test_bridge_release_rejects_in_vault_symlink_identity_for_dependency(
    vault: Path,
) -> None:
    bridge, source = _write_bridge_fixture(vault, approval=True)
    alias_path = source.with_name("private-source-alias.md")
    try:
        alias_path.symlink_to(source)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    release = _release_document()
    release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    release = release.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    release = release.replace("c" * 64, _restriction_signature(vault), 1)
    release = release.replace(SOURCE_PATH, alias_path.relative_to(vault).as_posix(), 1)
    _write_policy(vault, "grants", "bridge-release", release)
    policy._CACHE.clear()

    with request_scope(_external()):
        assert egress.release_level_for(vault, BRIDGE_PATH) == 0


def test_replace_memory_bridge_draft_requires_the_exact_reviewed_metadata(
    vault: Path,
) -> None:
    source = vault / SOURCE_PATH
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(_bridge_source_text(), encoding="utf-8")
    _write_policy(vault, "scopes", "private", _scope_document(SCOPE_A))
    _write_policy(vault, "rules", "private", _rule_document(scope_ids=(SCOPE_A,), ceiling=0))
    kwargs = {
        "old_path": "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization.md",
        "content": "## Claim\n\nDo not assume unlimited evening capacity.\n",
        "title": "Reviewed workload bridge",
        "slug": "reviewed-workload-bridge",
        "bridge_of": [SOURCE_PATH],
        "bridge_scope": "workload-planning",
        "bridge_review": "2026-12-01",
    }
    draft = commands.op_replace_memory(vault, validate_only=True, **kwargs)
    changed = commands.op_replace_memory(
        vault,
        validate_only=True,
        **{**kwargs, "bridge_scope": "capacity-planning"},
        draft_id=draft["draft_id"],
        draft_token=draft["draft_token"],
    )

    assert draft["draft_hash"] != changed["draft_hash"]
    with pytest.raises(ValueError, match="DRAFT_HASH_MISMATCH"):
        commands.op_replace_memory(
            vault,
            **{**kwargs, "bridge_scope": "capacity-planning"},
            draft_id=draft["draft_id"],
            draft_hash=draft["draft_hash"],
            draft_token=draft["draft_token"],
        )
    committed = commands.op_replace_memory(
        vault,
        **kwargs,
        draft_id=draft["draft_id"],
        draft_hash=draft["draft_hash"],
        draft_token=draft["draft_token"],
    )

    raw = (vault / committed["new_path"]).read_text(encoding="utf-8")
    assert "bridge_scope: workload-planning" in raw
    assert policy.load(vault).release_grants == ()
    with request_scope(_external()), pytest.raises(ValueError, match="^NOT_FOUND"):
        commands.op_get(vault, path=committed["new_path"])


@pytest.mark.parametrize(
    ("crash_at", "terminal"),
    [("after_intent", "aborted"), ("after_prepare", "committed")],
)
def test_bridge_release_recovery_is_exact_and_never_replays_semantics(
    vault: Path, monkeypatch: pytest.MonkeyPatch, crash_at: str, terminal: str
) -> None:
    from exomem.governance import receipts
    from exomem.governance.tool import (
        GovernanceCrash,
        op_govern_memory,
        reconcile_governance_operations,
    )

    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(vault.parent / "writer-state"))
    bridge, source = _write_bridge_fixture(vault, approval=False)
    release = _release_document()
    release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    release = release.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    release = release.replace("c" * 64, _restriction_signature(vault), 1)
    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Release the exact reviewed workload bridge",
        documents={"grants/bridge-release.yaml": release},
        selector_paths=[BRIDGE_PATH],
        target_ceiling=6,
        duration="standing",
    )
    with pytest.raises(GovernanceCrash):
        op_govern_memory(
            vault,
            operation="commit",
            principal=owner_principal(),
            proposal_id=proposal["proposal_id"],
            crash_at=crash_at,
        )
    before = (vault / "Knowledge Base/_Governance/grants/bridge-release.yaml").read_bytes() if (
        vault / "Knowledge Base/_Governance/grants/bridge-release.yaml"
    ).exists() else None
    result = reconcile_governance_operations(vault)
    after = vault / "Knowledge Base/_Governance/grants/bridge-release.yaml"
    records = receipts.event_records(vault)
    intent = next(row for row in records if row.get("phase") == "intent")
    terminals = [
        row for row in records if row.get("causation_id") == intent["event_id"] and row.get("phase") in {"committed", "aborted"}
    ]

    assert result["blocked"] is False
    assert [row["phase"] for row in terminals] == [terminal]
    if terminal == "aborted":
        assert before is None and not after.exists()
    else:
        assert before == after.read_bytes()
        assert policy.load(vault).release_grants[0].id == RELEASE_ID


@pytest.mark.parametrize("stale", [False, True], ids=["unapproved", "stale"])
def test_bridge_withholding_reaches_public_read_find_pack_graph_and_terminal_surfaces(
    vault: Path, stale: bool
) -> None:
    bridge, _source = _write_bridge_fixture(vault, approval=stale)
    if stale:
        bridge.write_text(
            bridge.read_text(encoding="utf-8").replace("unlimited", "forbidden"),
            encoding="utf-8",
        )
    with request_scope(_external()):
        for read in (
            lambda: commands.op_fetch(vault, id=BRIDGE_PATH),
            lambda: commands.op_read_memory(vault, path=BRIDGE_PATH),
        ):
            with pytest.raises(ValueError, match="^NOT_FOUND"):
                read()
        found = commands.op_find(
            vault, query="unlimited evening capacity", mode="keyword", graph=True, limit=10, pack=True
        )
        unit_hits = commands.op_find(
            vault,
            query="unlimited evening capacity",
            mode="keyword",
            graph=True,
            result_level="unit",
            limit=10,
        )
        mixed_hits = commands.op_find(
            vault,
            query="unlimited evening capacity",
            mode="keyword",
            graph=True,
            result_level="mixed",
            limit=10,
        )
        graph = commands.op_graph_context(vault, path=BRIDGE_PATH)

    for response in (found, unit_hits, mixed_hits):
        assert BRIDGE_PATH not in json.dumps(response, sort_keys=True, default=str)
    assert BRIDGE_PATH not in json.dumps(graph, sort_keys=True, default=str)


def test_bridge_review_triage_never_authorizes_and_exact_reapproval_clears_stale_cause(
    vault: Path,
) -> None:
    from exomem.governance.tool import op_govern_memory

    bridge, source = _write_bridge_fixture(vault, approval=True)
    source.write_text(_bridge_source_text(marker="changed after approval"), encoding="utf-8")
    report = attention.attention(vault, today=dt.date(2026, 7, 28), limit=0)
    item = next(item for item in report.items if "bridge_review" in item.categories)
    reason = next(reason for reason in item.reasons if reason["category"] == "bridge_review")
    assert reason["meta"]["cause"] == "SOURCE_CHANGED_OR_RESTRICTION_CHANGED"
    review_state.ReviewStateStore(vault).apply(item.item_id, item.fingerprint, action="dismiss")
    review_state.ReviewStateStore(vault).apply(
        item.item_id, item.fingerprint, action="snooze", until="2026-08-01"
    )
    with request_scope(_external()), pytest.raises(ValueError, match="^NOT_FOUND"):
        commands.op_get(vault, path=BRIDGE_PATH)

    release = _release_document()
    release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    release = release.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    release = release.replace("c" * 64, _restriction_signature(vault), 1)
    proposal = op_govern_memory(
        vault,
        operation="propose",
        principal=owner_principal(),
        intent="Reapprove the exact changed bridge dependency",
        documents={"grants/bridge-release.yaml": release},
        selector_paths=[BRIDGE_PATH],
        target_ceiling=6,
        duration="standing",
    )
    op_govern_memory(
        vault,
        operation="commit",
        principal=owner_principal(),
        proposal_id=proposal["proposal_id"],
    )

    with request_scope(_external()):
        assert commands.op_get(vault, path=BRIDGE_PATH)["path"] == BRIDGE_PATH
    assert not any(
        "bridge_review" in candidate.categories
        for candidate in attention.attention(vault, today=dt.date(2026, 7, 28), limit=0).items
    )


def test_quoted_bridge_keys_revalidate_hot_cache_when_dependency_changes(
    vault: Path,
) -> None:
    bridge, source = _write_bridge_fixture(vault, approval=False)
    bridge.write_text(
        bridge.read_text(encoding="utf-8")
        .replace("bridge_of:", "'bridge_of':")
        .replace("bridge_scope:", '"bridge_scope":')
        .replace("bridge_review:", "'bridge_review':"),
        encoding="utf-8",
    )
    release = _release_document()
    release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    release = release.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    release = release.replace("c" * 64, _restriction_signature(vault), 1)
    _write_policy(vault, "grants", "bridge-release", release)
    policy._CACHE.clear()

    with request_scope(_external()):
        first = commands.op_find(
            vault,
            query="unlimited evening capacity",
            mode="keyword",
            graph=False,
            limit=10,
        )
        assert any(hit.get("path") == BRIDGE_PATH for hit in first)
        before = source.stat()
        original = source.read_text(encoding="utf-8")
        edited = original.replace("RESTRICTED-SENTINEL", "DIFFERENT--SENTINEL")
        assert len(edited.encode()) == len(original.encode())
        source.write_text(edited, encoding="utf-8")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        second = commands.op_find(
            vault,
            query="unlimited evening capacity",
            mode="keyword",
            graph=False,
            limit=10,
        )

    assert all(hit.get("path") != BRIDGE_PATH for hit in second)


def test_release_redacts_dependency_title_embedded_in_direct_and_search_prose(
    vault: Path,
) -> None:
    sentinel = "RESTRICTED-PROSE-TITLE"
    bridge, source = _write_bridge_fixture(
        vault, approval=False, source_title=sentinel
    )
    bridge.write_text(
        bridge.read_text(encoding="utf-8")
        + f"\nThe evidence was compiled from {sentinel}, not a generic source.\n",
        encoding="utf-8",
    )
    release = _release_document()
    release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    release = release.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    release = release.replace("c" * 64, _restriction_signature(vault), 1)
    _write_policy(vault, "grants", "bridge-release", release)
    policy._CACHE.clear()

    with request_scope(_external()):
        page = commands.op_get(vault, path=BRIDGE_PATH)
        hits = commands.op_find(
            vault,
            query="generic source",
            mode="keyword",
            graph=False,
            limit=10,
        )

    for wire in (
        json.dumps(page, sort_keys=True, default=str).casefold(),
        json.dumps(hits, sort_keys=True, default=str).casefold(),
    ):
        assert sentinel.casefold() not in wire
    assert "not a generic source" in page["body"]


def test_owner_bridge_review_uses_approved_audience_and_hides_it_from_wire(
    vault: Path,
) -> None:
    _write_bridge_fixture(vault, approval=True, review="2026-01-01")
    raw = attention.attention(
        vault,
        categories=["bridge_review"],
        today=dt.date(2026, 7, 28),
        limit=0,
    ).as_dict()

    with request_scope(owner_principal()):
        owner = egress.filter_withheld_entries(vault, raw)
    with request_scope(_external("other-audience")):
        other = egress.filter_withheld_entries(vault, raw)

    assert len(owner["items"]) == 1
    assert owner["summary"] == {"bridge_review": 1}
    assert owner["shown"] == owner["total"] == owner["all_total"] == 1
    owner_wire = json.dumps(owner, sort_keys=True, default=str).casefold()
    assert "external" not in owner_wire
    assert SOURCE_PATH.casefold() not in owner_wire
    assert SOURCE_REF.casefold() not in owner_wire
    assert other["items"] == []
    assert other["summary"] == {}
    assert other["shown"] == other["total"] == other["all_total"] == 0


def test_release_redacts_short_title_and_encoded_path_aliases_in_direct_and_search_prose(
    vault: Path,
) -> None:
    bridge, source = _write_bridge_fixture(vault, approval=False, source_title="Apollo")
    encoded_source_path = SOURCE_PATH.replace(" ", "%20")
    bridge.write_text(
        bridge.read_text(encoding="utf-8")
        + (
            f"\nApollo is protected; see {encoded_source_path}. "
            "The canonical path is Notes/Research/Health/private-source and "
            "the stem is private-source. "
            "ApolloX remains an unrelated token.\n"
        ),
        encoding="utf-8",
    )
    release = _release_document()
    release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    release = release.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    release = release.replace("c" * 64, _restriction_signature(vault), 1)
    _write_policy(vault, "grants", "bridge-release", release)
    policy._CACHE.clear()

    with request_scope(_external()):
        page = commands.op_get(vault, path=BRIDGE_PATH)
        hits = commands.op_find(
            vault,
            query="unrelated token",
            mode="keyword",
            graph=False,
            limit=10,
        )

    for wire in (
        json.dumps(page, sort_keys=True, default=str).casefold(),
        json.dumps(hits, sort_keys=True, default=str).casefold(),
    ):
        assert not re.search(r"(?<![a-z0-9])apollo(?![a-z0-9])", wire)
        assert encoded_source_path.casefold() not in wire
        assert "notes/research/health/private-source" not in wire
        assert not re.search(r"(?<![a-z0-9])private-source(?![a-z0-9])", wire)
    assert "apollox remains an unrelated token" in page["body"].casefold()


def test_owner_bridge_review_retains_stale_release_without_audience_leak(
    vault: Path,
) -> None:
    _bridge, source = _write_bridge_fixture(vault, approval=True)
    source.write_text(_bridge_source_text(marker="changed after approval"), encoding="utf-8")
    raw = attention.attention(
        vault,
        categories=["bridge_review"],
        today=dt.date(2026, 7, 28),
        limit=0,
    ).as_dict()

    with request_scope(owner_principal()):
        owner = egress.filter_withheld_entries(vault, raw)
    with request_scope(_external("other-audience")):
        other = egress.filter_withheld_entries(vault, raw)

    assert len(owner["items"]) == 1
    assert owner["summary"] == {"bridge_review": 1}
    assert owner["shown"] == owner["total"] == owner["all_total"] == 1
    owner_wire = json.dumps(owner, sort_keys=True, default=str).casefold()
    assert "external" not in owner_wire
    assert SOURCE_PATH.casefold() not in owner_wire
    assert SOURCE_REF.casefold() not in owner_wire
    assert other["items"] == []
    assert other["summary"] == {}
    assert other["shown"] == other["total"] == other["all_total"] == 0


def test_release_redacts_fully_and_double_encoded_dependency_aliases(
    vault: Path,
) -> None:
    bridge, source = _write_bridge_fixture(vault, approval=False, source_title="Apollo")
    encoded_path = quote(SOURCE_PATH, safe="")
    double_encoded_path = quote(encoded_path, safe="")
    encoded_ref = quote(SOURCE_REF, safe="")
    bridge.write_text(
        bridge.read_text(encoding="utf-8")
        + (
            f"\nFully encoded aliases: {encoded_path}; {encoded_ref}; "
            f"{double_encoded_path}. Unrelated text remains.\n"
        ),
        encoding="utf-8",
    )
    release = _release_document()
    release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    release = release.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    release = release.replace("c" * 64, _restriction_signature(vault), 1)
    _write_policy(vault, "grants", "bridge-release", release)
    policy._CACHE.clear()

    with request_scope(_external()):
        page = commands.op_get(vault, path=BRIDGE_PATH)
        hits = commands.op_find(
            vault,
            query="unrelated text",
            mode="keyword",
            graph=False,
            limit=10,
        )

    for wire in (
        json.dumps(page, sort_keys=True, default=str).casefold(),
        json.dumps(hits, sort_keys=True, default=str).casefold(),
    ):
        assert encoded_path.casefold() not in wire
        assert double_encoded_path.casefold() not in wire
        assert encoded_ref.casefold() not in wire
    assert "unrelated text remains" in page["body"].casefold()


def test_release_redacts_mixed_depth_encoded_dependency_aliases(
    vault: Path,
) -> None:
    bridge, source = _write_bridge_fixture(vault, approval=False, source_title="Apollo")
    mixed_path = SOURCE_PATH.replace(" ", "%2520").replace("/", "%2F")
    mixed_ref = SOURCE_REF.replace(":", "%253A").replace("/", "%2F")
    bridge.write_text(
        bridge.read_text(encoding="utf-8")
        + f"\nMixed aliases: {mixed_path}; {mixed_ref}. Unrelated text remains.\n",
        encoding="utf-8",
    )
    release = _release_document()
    release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    release = release.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    release = release.replace("c" * 64, _restriction_signature(vault), 1)
    _write_policy(vault, "grants", "bridge-release", release)
    policy._CACHE.clear()

    with request_scope(_external()):
        page = commands.op_get(vault, path=BRIDGE_PATH)
        hits = commands.op_find(
            vault,
            query="unrelated text",
            mode="keyword",
            graph=False,
            limit=10,
        )

    for wire in (
        json.dumps(page, sort_keys=True, default=str).casefold(),
        json.dumps(hits, sort_keys=True, default=str).casefold(),
    ):
        assert mixed_path.casefold() not in wire
        assert mixed_ref.casefold() not in wire
    assert "unrelated text remains" in page["body"].casefold()


def test_release_redacts_encoded_delimiters_without_redacting_encoded_superstrings(
    vault: Path,
) -> None:
    bridge, source = _write_bridge_fixture(vault, approval=False, source_title="Apollo")
    eight_depth_apollo = "".join(f"%{byte:02X}" for byte in b"Apollo")
    for _ in range(7):
        eight_depth_apollo = quote(eight_depth_apollo, safe="")
    aliases = (
        "%2FApollo",
        "%28Apollo%29",
        "%2fApollo",
        eight_depth_apollo,
    )
    bridge.write_text(
        bridge.read_text(encoding="utf-8")
        + (
            "\nEncoded boundary aliases: "
            + "; ".join(aliases)
            + ". Apollo%58 and ApolloX are unrelated tokens.\n"
        ),
        encoding="utf-8",
    )
    release = _release_document()
    release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    release = release.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    release = release.replace("c" * 64, _restriction_signature(vault), 1)
    _write_policy(vault, "grants", "bridge-release", release)
    policy._CACHE.clear()

    with request_scope(_external()):
        page = commands.op_get(vault, path=BRIDGE_PATH)
        hits = commands.op_find(
            vault,
            query="unrelated tokens",
            mode="keyword",
            graph=False,
            limit=10,
        )

    for wire in (
        json.dumps(page, sort_keys=True, default=str).casefold(),
        json.dumps(hits, sort_keys=True, default=str).casefold(),
    ):
        for alias in aliases:
            assert alias.casefold() not in wire
        assert "apollo%58" in wire
        assert "apollox" in wire
        decoded = wire
        for _ in range(8):
            decoded = unquote(decoded)
        assert not re.search(r"(?<!\w)apollo(?!\w)", decoded, re.IGNORECASE)
        assert "apollox" in decoded


def test_provenance_shadow_decode_preserves_malformed_percent_and_near_misses() -> None:
    identity = bridges.StripIdentity(
        path="Knowledge Base/Notes/Research/cafe.md",
        ref="exomem://memory/cafe",
        title="café",
    )
    payload = {
        "body": "%2Fcaf%C3%A9 and %63%61%66%C3%A9; caféX; 100%; malformed %2G."
    }

    cleaned = bridges.strip_provenance(payload, (identity,))

    decoded = cleaned["body"]
    for _ in range(8):
        decoded = unquote(decoded)
    assert not re.search(r"(?<!\w)café(?!\w)", decoded, re.IGNORECASE)
    assert "caféX" in decoded
    assert "100%" in cleaned["body"]
    assert "malformed %2G" in cleaned["body"]


def test_provenance_reference_context_uses_exact_aliases_not_substrings() -> None:
    identity = bridges.StripIdentity(
        path="Knowledge Base/Notes/Research/Apollo.md",
        ref="exomem://memory/apollo",
        title="Apollo",
    )
    payload = {
        "relations": [
            {
                "source_title": "ApolloX",
                "candidate_id": "ApolloX",
                "excerpt": "ApolloX is unrelated.",
            },
            {"source_title": "Apollo"},
        ]
    }

    cleaned = bridges.strip_provenance(payload, (identity,))

    assert cleaned["relations"] == [
        {
            "source_title": "ApolloX",
            "candidate_id": "ApolloX",
            "excerpt": "ApolloX is unrelated.",
        }
    ]


def test_release_redacts_aliases_adjacent_to_invalid_percent_bytes(
    vault: Path,
) -> None:
    bridge, source = _write_bridge_fixture(vault, approval=False, source_title="Apollo")
    aliases = ("%FF%41%70%6F%6C%6C%6F", "%2F%FFApollo")
    bridge.write_text(
        bridge.read_text(encoding="utf-8")
        + "\nInvalid-prefix aliases: "
        + "; ".join(aliases)
        + ". Unrelated text remains.\n",
        encoding="utf-8",
    )
    release = _release_document()
    release = release.replace(SHA_A, hashlib.sha256(bridge.read_bytes()).hexdigest(), 1)
    release = release.replace(SHA_B, hashlib.sha256(source.read_bytes()).hexdigest(), 1)
    release = release.replace("c" * 64, _restriction_signature(vault), 1)
    _write_policy(vault, "grants", "bridge-release", release)
    policy._CACHE.clear()

    with request_scope(_external()):
        page = commands.op_get(vault, path=BRIDGE_PATH)
        hits = commands.op_find(
            vault,
            query="unrelated text",
            mode="keyword",
            graph=False,
            limit=10,
        )

    for wire in (
        json.dumps(page, sort_keys=True, default=str).casefold(),
        json.dumps(hits, sort_keys=True, default=str).casefold(),
    ):
        decoded = wire
        for _ in range(8):
            decoded = unquote(decoded)
        assert not re.search(r"(?<!\w)apollo(?!\w)", decoded, re.IGNORECASE)
        assert "unrelated text remains" in decoded
        assert "%ff" in wire


@pytest.mark.parametrize("title", ["restricted dependency", "[restricted dependency]"])
def test_provenance_redaction_never_recreates_a_dependency_title(title: str) -> None:
    identity = bridges.StripIdentity(
        path="Knowledge Base/Notes/Research/private.md",
        ref="exomem://memory/private",
        title=title,
    )

    cleaned = bridges.strip_provenance({"body": f"Before {title} after."}, (identity,))

    decoded = cleaned["body"]
    for _ in range(8):
        decoded = unquote(decoded)
    assert title.casefold() not in decoded.casefold()


def test_provenance_canonicalizes_percent_encoded_dependency_identities() -> None:
    identity = bridges.StripIdentity(
        path="Knowledge Base/Notes/Research/literal%20path.md",
        ref="exomem://memory/Budget%20Plan",
        title="Budget %20 Plan",
    )
    payload = {
        "body": (
            "Budget %20 Plan; Knowledge Base/Notes/Research/literal%20path.md; "
            "exomem://memory/Budget%20Plan."
        )
    }

    cleaned = bridges.strip_provenance(payload, (identity,))

    decoded = cleaned["body"]
    for _ in range(8):
        decoded = unquote(decoded)
    for alias in (
        "Budget   Plan",
        "Knowledge Base/Notes/Research/literal path.md",
        "exomem://memory/Budget Plan",
    ):
        assert alias.casefold() not in decoded.casefold()


def test_provenance_compiles_alias_patterns_once_per_strip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = tuple(
        bridges.StripIdentity(
            path=f"Knowledge Base/Notes/Research/dependency-{index}.md",
            ref=f"exomem://memory/dependency-{index}",
            title=f"Dependency {index}",
        )
        for index in range(100)
    )
    aliases = bridges._embedded_identity_aliases(identities)
    payload = {"entries": [{"excerpt": "ordinary prose"} for _ in range(30)]}
    original_compile = bridges.re.compile
    calls = 0

    def counting_compile(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(bridges.re, "compile", counting_compile)

    assert bridges.strip_provenance(payload, identities) == payload
    assert calls == len(aliases)


@pytest.mark.parametrize("title", ["%20", "%09"])
def test_release_refuses_dependency_identity_that_canonicalizes_empty(
    vault: Path, title: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _source = _write_bridge_fixture(vault, approval=True)
    original_snapshot = bridges._dependency_snapshot

    def empty_title_snapshot(*args, **kwargs):
        live = original_snapshot(*args, **kwargs)
        assert live is not None
        return (*live[:3], title, live[4])

    monkeypatch.setattr(bridges, "_dependency_snapshot", empty_title_snapshot)

    admission = bridges.admit(
        vault,
        BRIDGE_PATH,
        bridge.read_bytes(),
        policy=policy.load(vault),
        audience="external",
    )
    with request_scope(_external()):
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=BRIDGE_PATH)

    assert admission.is_bridge
    assert not admission.allowed
    assert admission.reason in {
        bridges.RELEASE_STALE,
        bridges.SOURCE_UNAVAILABLE_OR_AMBIGUOUS,
    }
