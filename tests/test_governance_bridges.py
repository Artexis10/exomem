"""Cross-domain bridge governance: exact releases, stripping, and review."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import unicodedata
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote

import pytest

from exomem import (
    attention,
    commands,
    context_pack,
    embeddings,
    epistemic_graph,
    find_results,
    freshness,
    reserved_paths,
    review_state,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.governance import bridges, decisions, egress, membership, policy
from exomem.governance.principal import (
    RequestPrincipal,
    library_scope,
    owner_principal,
    request_scope,
)
from exomem.writer_lease import invoke_command

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
APPROVED_BRIDGE_ABSTRACTION = (
    "# Workload constraint\n\n"
    "## Observations\n"
    "- [constraint] Do not assume unlimited evening capacity #planning ^capacity\n\n"
    "## Relations\n"
)


@pytest.fixture(autouse=True)
def _clear_policy_caches():
    with reserved_paths._owner_authority_scope("govern_memory"), library_scope():
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
    source_path: str = SOURCE_PATH,
    padding: str = "",
    relations: str | None = None,
    sources: str | None = None,
    lede: str = "",
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
        + (sources or f"  - '[[{source_path.removesuffix('.md')}]]'\n")
        + f"bridge_of: [{SOURCE_REF!r}]\n"
        "bridge_scope: workload-planning\n"
        f"bridge_review: {review}\n"
        "tags: [planning]\n"
        "---\n\n"
        "# Workload constraint\n\n"
        f"{lede}"
        "## Observations\n"
        "- [constraint] Do not assume unlimited evening capacity #planning ^capacity\n\n"
        f"{padding}"
        "## Relations\n"
        + (relations or f"- derived_from [[{source_path.removesuffix('.md')}]]\n")
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
    source_ceiling: int = 0,
    source_rule_extra: str = "",
    bridge_path: str = BRIDGE_PATH,
    source_path: str = SOURCE_PATH,
    bridge_suffix: str = "",
    bridge_padding: str = "",
    bridge_relations: str | None = None,
    bridge_sources: str | None = None,
    bridge_lede: str = "",
) -> tuple[Path, Path]:
    source = vault / source_path
    bridge = vault / bridge_path
    source.parent.mkdir(parents=True, exist_ok=True)
    bridge.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(_bridge_source_text(title=source_title), encoding="utf-8")
    bridge.write_text(
        _bridge_text(
            review=review,
            source_path=source_path,
            padding=bridge_padding,
            relations=bridge_relations,
            sources=bridge_sources,
            lede=bridge_lede,
        )
        + bridge_suffix,
        encoding="utf-8",
    )
    _write_policy(vault, "scopes", "private", _scope_document(SCOPE_A, path=source_path))
    _write_policy(
        vault,
        "rules",
        "private",
        _rule_document(
            scope_ids=(SCOPE_A,),
            ceiling=source_ceiling,
            extra=source_rule_extra,
        ),
    )
    if approval:
        release = _release_document()
        release = release.replace(BRIDGE_PATH, bridge_path).replace(SOURCE_PATH, source_path)
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


def test_default_deny_does_not_hide_a_bridge_release_reason_from_inspection(
    vault: Path,
) -> None:
    """The existence-oracle guard owns pure default denials, not the bridge
    state's established content-free explanation contract."""
    _write_bridge_fixture(vault, approval=False)
    _write_policy(
        vault,
        "scopes",
        "closed-bridge",
        _scope_document(SCOPE_B, path=BRIDGE_PATH) + "default_deny: true\n",
    )

    with request_scope(_external()):
        explained = commands.op_govern_memory(
            vault,
            operation="explain",
            path=BRIDGE_PATH,
            audience="external",
        )
        simulated = commands.op_govern_memory(
            vault,
            operation="simulate",
            paths=[BRIDGE_PATH],
            audience="external",
        )

    assert explained["effective_ceiling"] == 0
    assert explained["release_reason"] == "RELEASE_UNAPPROVED"
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


@pytest.mark.parametrize("bridge_suffix", ["", "<!-- exomem-origin:v2 PRIVATE-ATTRIBUTION -->"])
def test_l4_source_renders_only_the_exact_approved_bridge_abstraction(
    vault: Path, bridge_suffix: str,
) -> None:
    _write_bridge_fixture(
        vault,
        approval=True,
        source_ceiling=egress.LEVEL_EXCERPT_REDACTED,
        bridge_suffix=bridge_suffix,
        source_rule_extra=(
            "options:\n"
            f"  bridge: {RELEASE_ID}\n"
            "  abstract: safe fallback abstraction\n"
        ),
    )

    with request_scope(_external()):
        page = commands.op_get(vault, path=SOURCE_PATH, include_raw=True)

    assert page == {
        "withheld": True,
        "level": egress.LEVEL_EXCERPT_REDACTED,
        "bridge": APPROVED_BRIDGE_ABSTRACTION,
    }
    assert RELEASE_ID not in str(page)
    assert SOURCE_PATH not in str(page)
    assert SOURCE_REF not in str(page)


def test_nonempty_policy_reads_logical_unicode_source_from_its_physical_spelling(
    vault: Path,
) -> None:
    logical_path = "Knowledge Base/Sources/café.md"
    source = vault / unicodedata.normalize("NFD", logical_path)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        _bridge_source_text(marker="ORDINARY-SOURCE").replace("type: research-note", "type: source"),
        encoding="utf-8",
    )
    _write_policy(vault, "scopes", "source", _scope_document(SCOPE_A, path=logical_path))
    _write_policy(vault, "rules", "source", _rule_document(scope_ids=(SCOPE_A,), ceiling=6))

    with request_scope(_external()):
        page = commands.op_get(vault, path=logical_path)

    assert page["path"] == logical_path
    assert "ORDINARY-SOURCE" in page["body"]


def test_exact_nfd_bridge_and_dependency_release_and_invalidate_together(vault: Path) -> None:
    bridge_path = unicodedata.normalize("NFD", "Knowledge Base/Notes/Insights/café.md")
    source_path = unicodedata.normalize("NFD", "Knowledge Base/Sources/privé.md")
    _bridge, source = _write_bridge_fixture(
        vault,
        approval=True,
        bridge_path=bridge_path,
        source_path=source_path,
        source_ceiling=egress.LEVEL_EXCERPT_REDACTED,
        source_rule_extra=(
            "options:\n"
            f"  bridge: {RELEASE_ID}\n"
            "  abstract: safe fallback abstraction\n"
        ),
    )
    compiled = policy.load(vault)
    grant = compiled.release_grants[0]

    with request_scope(_external()):
        page = commands.op_get(vault, path=bridge_path)
        abstraction = commands.op_get(vault, path=source_path, include_raw=True)
    due = bridges.review_signal(vault, grant, policy=compiled, today=dt.date(2026, 12, 1))

    assert page["path"] == bridge_path
    assert page["body"] == APPROVED_BRIDGE_ABSTRACTION
    assert source_path not in json.dumps(page, ensure_ascii=False, default=str)
    assert SOURCE_REF not in str(page)
    assert abstraction == {
        "withheld": True,
        "level": egress.LEVEL_EXCERPT_REDACTED,
        "bridge": APPROVED_BRIDGE_ABSTRACTION,
    }
    assert due is not None and due.cause == bridges.DUE_REVIEW

    source.write_text(_bridge_source_text(marker="DEPENDENCY-EDITED"), encoding="utf-8")
    with request_scope(_external()):
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=bridge_path)
        lowered = commands.op_get(vault, path=source_path, include_raw=True)
    changed = bridges.review_signal(vault, grant, policy=compiled, today=dt.date(2026, 12, 2))

    assert lowered["level"] == egress.LEVEL_ABSTRACT
    assert lowered["abstract"] == "safe fallback abstraction"
    assert "bridge" not in lowered
    assert changed is not None and changed.cause == bridges.SOURCE_CHANGED_OR_RESTRICTION_CHANGED
    assert changed.signal_version != due.signal_version


def test_equivalent_bridge_leaves_with_distinct_ids_refuse_l4_release(vault: Path) -> None:
    bridge_path = "Knowledge Base/Notes/Insights/café.md"
    bridge, _source = _write_bridge_fixture(
        vault,
        approval=True,
        bridge_path=bridge_path,
        source_ceiling=egress.LEVEL_EXCERPT_REDACTED,
        source_rule_extra=(
            "options:\n"
            f"  bridge: {RELEASE_ID}\n"
            "  abstract: safe fallback abstraction\n"
        ),
    )
    other = vault / unicodedata.normalize("NFD", bridge_path)
    other.write_text(
        bridge.read_text(encoding="utf-8").replace(
            "00000000-0000-4000-8000-000000000201", "00000000-0000-4000-8000-000000000203"
        ),
        encoding="utf-8",
    )

    with request_scope(_external()):
        page = commands.op_get(vault, path=SOURCE_PATH)
        with pytest.raises(ValueError, match="^AMBIGUOUS_PATH"):
            commands.op_get(vault, path=bridge_path)

    assert page["level"] == egress.LEVEL_ABSTRACT
    assert page["abstract"] == "safe fallback abstraction"
    assert "bridge" not in page


def test_l4_bridge_read_refuses_a_published_private_identity(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _source = _write_bridge_fixture(
        vault,
        approval=True,
        source_ceiling=egress.LEVEL_EXCERPT_REDACTED,
        source_rule_extra=(
            "options:\n"
            f"  bridge: {RELEASE_ID}\n"
            "  abstract: safe fallback abstraction\n"
        ),
    )
    identity = reserved_paths._lstat_identity(bridge)
    with reserved_paths._subsystem_authority_scope("embedding_index"):
        with reserved_paths._identity_coordination_scope(vault):
            reserved_paths._publish_owner_identities(
                vault, "embeddings-store", {"Knowledge Base/.embeddings.sqlite": identity}
            )
    monkeypatch.setattr(
        reserved_paths,
        "_published_identity_catalogue",
        lambda _root: reserved_paths.IdentityCatalogue(
            {(identity.device, identity.inode, identity.kind): "embeddings-store"}
        ),
    )

    with request_scope(_external()):
        page = commands.op_get(vault, path=SOURCE_PATH)

    assert page["level"] == egress.LEVEL_ABSTRACT
    assert page["abstract"] == "safe fallback abstraction"
    assert "bridge" not in page


def test_renamed_bridge_stays_stale_and_readable_malformed_edits_change_review_version(
    vault: Path,
) -> None:
    bridge_path = "Knowledge Base/Notes/Insights/café.md"
    bridge, _source = _write_bridge_fixture(vault, approval=True, bridge_path=bridge_path)
    compiled = policy.load(vault)
    grant = compiled.release_grants[0]
    renamed = bridge.rename(vault / unicodedata.normalize("NFD", bridge_path))

    with request_scope(_external()):
        with pytest.raises(ValueError, match="^NOT_FOUND"):
            commands.op_get(vault, path=bridge_path)
    renamed_signal = bridges.review_signal(
        vault, grant, policy=compiled, today=dt.date(2026, 12, 1)
    )
    assert renamed_signal is not None and renamed_signal.cause == bridges.BRIDGE_EDITED

    renamed.write_bytes(b"---\nbridge_of: [\xff]\n---\nfirst malformed edit\n")
    first = bridges.review_signal(vault, grant, policy=compiled, today=dt.date(2026, 12, 1))
    renamed.write_bytes(b"---\nbridge_of: [\xfe]\n---\nsecond malformed edit\n")
    second = bridges.review_signal(vault, grant, policy=compiled, today=dt.date(2026, 12, 1))

    assert first is not None and first.cause == bridges.BRIDGE_EDITED
    assert second is not None and second.cause == bridges.BRIDGE_EDITED
    assert first.bridge_hash != second.bridge_hash
    assert first.signal_version != second.signal_version


def test_l4_source_bridge_edit_lowers_without_reusing_cached_approval(
    vault: Path,
) -> None:
    bridge, _source = _write_bridge_fixture(
        vault,
        approval=True,
        source_ceiling=egress.LEVEL_EXCERPT_REDACTED,
        source_rule_extra=(
            "options:\n"
            f"  bridge: {RELEASE_ID}\n"
            "  abstract: safe fallback abstraction\n"
        ),
    )
    with request_scope(_external()):
        approved = commands.op_get(vault, path=SOURCE_PATH, include_raw=True)
    assert approved["bridge"] == APPROVED_BRIDGE_ABSTRACTION

    bridge.write_text(
        _bridge_text().replace("unlimited evening capacity", "changed private bridge"),
        encoding="utf-8",
    )
    find_module.clear_cache()

    with request_scope(_external()):
        page = commands.op_get(vault, path=SOURCE_PATH, include_raw=True)

    assert page == {
        "withheld": True,
        "level": egress.LEVEL_ABSTRACT,
        "rule_ids": [RULE_ID],
        "scope_label": SCOPE_A,
        "abstract": "safe fallback abstraction",
    }
    assert RELEASE_ID not in str(page)
    assert "changed private bridge" not in str(page)


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


_SOURCE_STEM = SOURCE_PATH.rsplit("/", 1)[-1].removesuffix(".md")
_PLAIN_MENTION = f"Compiled from {_SOURCE_STEM}.\n"


def _cap_bridge_page_at_excerpt_level(vault: Path) -> None:
    """Cap the bridge page itself at L5, so a read of it returns the bounded body."""
    _write_policy(vault, "scopes", "bridge", _scope_document(SCOPE_B, path=BRIDGE_PATH))
    _write_policy(
        vault,
        "rules",
        "bridge",
        _rule_document(
            scope_ids=(SCOPE_B,),
            ceiling=egress.LEVEL_EXCERPT,
            rule_id="01ARZ3NDEKTSV4RRFFQ69G5FBD",
        ),
    )
    policy._CACHE.clear()


# `zebra` anchors the excerpt a hundred characters past the link, so a length cut starts inside it.
# `capacity` on a short page gives an excerpt that is the whole page, with no cut at all.
_EXCERPT_UNDER_STRIP = [
    pytest.param(
        "zebra", f"\n[the source]({SOURCE_PATH}) for detail. " + "filler " * 10 + "zebra\n", None,
        id="cut-inside-a-link",
    ),
    pytest.param("capacity", "", _PLAIN_MENTION, id="uncut"),
]


@pytest.mark.parametrize("detail", ["compact", "full"])
@pytest.mark.parametrize(("query", "suffix", "relations"), _EXCERPT_UNDER_STRIP)
def test_a_hit_under_a_release_strip_carries_no_excerpt(
    vault: Path, detail: str, query: str, suffix: str, relations: str | None
) -> None:
    _write_bridge_fixture(
        vault, approval=True, bridge_suffix=suffix, bridge_relations=relations
    )

    with request_scope(_external()):
        found = commands.op_ask_memory(
            vault, query=query, mode="keyword", limit=10, detail=detail
        )

    bridge_hit = next(hit for hit in found["hits"] if hit["path"] == BRIDGE_PATH)
    assert "excerpt" not in bridge_hit
    wire = json.dumps(bridge_hit, sort_keys=True).casefold()
    for length in range(4, len(_SOURCE_STEM) + 1):
        assert _SOURCE_STEM[:length] not in wire, _SOURCE_STEM[:length]
        assert _SOURCE_STEM[-length:] not in wire, _SOURCE_STEM[-length:]


def test_a_vector_chunk_that_starts_inside_a_link_is_withheld_under_a_release_strip() -> None:
    # An unspaced paragraph is split at sentence marks and hard cuts, so a chunk can start inside
    # a link, and the excerpt of such a chunk carries no cut mark.
    body = "这是一个关于工作量约束的说明" * 32 + (
        "见[[Knowledge Base/Notes/Research/Health/private source note]]之后的内容很短。"
    )
    chunk = embeddings.chunk_text("Workload constraint", body)[1]
    page = SimpleNamespace(search_body="", body="", title="Workload constraint")
    excerpt = find_results.semantic_excerpt(page, "", chunk, None)
    assert excerpt.startswith("source note]]")
    identity = bridges.StripIdentity(
        path="Knowledge Base/Notes/Research/Health/private source note.md",
        ref=SOURCE_REF,
        title="Private source title",
    )

    assert bridges.strip_provenance({"excerpt": excerpt}, (identity,)) == {}


def test_a_cut_excerpt_is_unchanged_in_an_ungoverned_vault(vault: Path) -> None:
    (vault / BRIDGE_PATH).parent.mkdir(parents=True, exist_ok=True)
    (vault / BRIDGE_PATH).write_text(
        _bridge_text()
        + f"\n{SOURCE_PATH} for detail. "
        + "filler " * 11
        + "zebra\n",
        encoding="utf-8",
    )
    find_module.clear_cache()

    with request_scope(_external()):
        found = commands.op_ask_memory(vault, query="zebra", mode="keyword", limit=10)

    excerpt = next(hit for hit in found["hits"] if hit["path"] == BRIDGE_PATH)["excerpt"]
    assert excerpt.startswith("…")
    assert "-source.md for detail." in excerpt


def test_a_truncated_read_body_of_a_page_under_a_release_strip_is_withheld(
    vault: Path,
) -> None:
    # The source name holds spaces, so the whole-word cut of the bounded body ends inside it.
    source_path = "Knowledge Base/Notes/Research/Health/private source note.md"
    bridge, _source = _write_bridge_fixture(
        vault,
        approval=True,
        source_path=source_path,
        source_ceiling=egress.LEVEL_EXCERPT,
        bridge_padding="filler " * 57 + "\n\n",
    )
    # The source is readable at the excerpt level, so only the bridge strip removes it.
    _cap_bridge_page_at_excerpt_level(vault)
    cut = egress._excerpt_of(bridge.read_text(encoding="utf-8").split("\n---\n", 1)[1])
    assert cut.endswith("private source …")

    with request_scope(_external()):
        page = commands.op_get(vault, path=BRIDGE_PATH)

    assert page["body_truncated"] is True
    assert "body" not in page
    wire = json.dumps(page, sort_keys=True, default=str).casefold()
    stem = "private source note"
    for length in range(4, len(stem) + 1):
        assert stem[:length] not in wire, stem[:length]


def test_an_uncut_read_body_of_a_page_under_a_release_strip_keeps_its_text_without_the_source(
    vault: Path,
) -> None:
    _write_bridge_fixture(
        vault,
        approval=True,
        source_ceiling=egress.LEVEL_EXCERPT,
        bridge_relations=_PLAIN_MENTION,
    )
    _cap_bridge_page_at_excerpt_level(vault)

    with request_scope(_external()):
        page = commands.op_get(vault, path=BRIDGE_PATH)

    assert page["body_truncated"] is False
    assert "Do not assume unlimited evening capacity" in page["body"]
    assert "private" not in page["body"].casefold()


# A stem that no other word of a pack shares, so a surviving fragment of it can only come from the
# source.
_TRAIL_STEM = "zqxv-lab-panel"
_TRAIL_DIRECTORY = "Knowledge Base/Notes/Research/Health/"
_TRAIL_PATH = f"{_TRAIL_DIRECTORY}{_TRAIL_STEM}.md"
_TRAIL_LINK = f"[[{_TRAIL_PATH.removesuffix('.md')}]]"
# A link starts with `[[` and the directory, so the stem begins this many characters into it.
_LINK_HEAD = len("[[") + len(_TRAIL_DIRECTORY)


def _lead_in(bound: int, word: str = "word ") -> str:
    """Filler that puts a following `_TRAIL_LINK` across `bound`: a cut at `bound` ends 4 to 8
    characters into the stem, whatever the bound is."""
    return word * ((bound - _LINK_HEAD - 4) // len(word))


_LONG_TRAIL_PATH = (
    f"{_TRAIL_DIRECTORY}{_lead_in(context_pack._MAX_UNIT_RELATION_TARGET_CHARS - 1, 'long-')}/"
    f"{_TRAIL_STEM}.md"
)
_TRAIL_DECISION = (
    "\n## Decision\n"
    "- id: cap-evening\n"
    "- category: planning\n"
    f"- relations: evidenced_by: {_TRAIL_LINK}\n"
    f"- context: Derived from {_TRAIL_LINK} in full.\n\n"
    f"Cap the evening load, as {_TRAIL_LINK} shows.\n"
)


def _ask_as_guest(vault: Path, query: str = "unlimited evening capacity", **arguments) -> dict:
    """What an external caller receives on the wire: through the dispatcher and its terminal filter."""
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "ask_memory")
    with request_scope(_external()):
        return invoke_command(
            command, vault, query=query, mode="keyword", limit=10, deep=True, **arguments
        )


def _assert_stem_absent(wire: object) -> None:
    text = json.dumps(wire, sort_keys=True, default=str).casefold()
    for length in range(4, len(_TRAIL_STEM) + 1):
        assert _TRAIL_STEM[:length] not in text, _TRAIL_STEM[:length]
        assert _TRAIL_STEM[-length:] not in text, _TRAIL_STEM[-length:]


def test_a_deep_pack_of_a_page_under_a_release_strip_carries_no_source_trail(
    vault: Path,
) -> None:
    _write_bridge_fixture(
        vault,
        approval=True,
        source_path=_TRAIL_PATH,
        bridge_suffix=_TRAIL_DECISION,
        bridge_relations="No further relations.\n",
    )

    found = _ask_as_guest(vault)

    entry = found["pack"]["semantic_units"][BRIDGE_PATH]
    compact, decision = entry["units"]
    assert entry["parent"]["sources"] == []
    assert decision["relations"] == []
    assert "context" not in decision
    assert found["pack"]["semantic_blocks"][BRIDGE_PATH][0]["relations"] == []
    assert "Decision" not in " ".join(found["pack"]["claims"][BRIDGE_PATH]["sections"])
    # The released claim itself stays.
    assert compact["excerpt"] == "Do not assume unlimited evening capacity"
    _assert_stem_absent(found)


# Each case puts the link where one builder's length cut ends inside the stem, and nowhere else in
# the page. The filler is sized from the builder's own bound, so a changed bound moves the case with it.
_DECISION_HEAD = "\n## Decision\n- id: cap-evening\n- category: planning\n"
_CUT_FIELD_UNDER_STRIP = [
    pytest.param(
        {
            "bridge_sources": f"  - '{_lead_in(context_pack._MAX_PARENT_PROVENANCE_CHARS)}{_TRAIL_LINK}'\n"
        },
        _TRAIL_PATH,
        id="parent-sources-entry",
    ),
    pytest.param(
        {
            "bridge_suffix": f"{_DECISION_HEAD}- context: "
            f"{_lead_in(context_pack._MAX_UNIT_CONTEXT_CHARS)}{_TRAIL_LINK}\n\n"
            "Cap the evening load.\n"
        },
        _TRAIL_PATH,
        id="unit-context",
    ),
    pytest.param(
        {
            "bridge_suffix": f"{_DECISION_HEAD}- note: "
            f"{_lead_in(context_pack._MAX_LEGACY_METADATA_VALUE_CHARS)}{_TRAIL_LINK}\n\n"
            "Cap the evening load.\n"
        },
        _TRAIL_PATH,
        id="block-metadata-value-and-section-lead",
    ),
    pytest.param(
        {
            "bridge_suffix": f"{_DECISION_HEAD}\n"
            f"{_lead_in(context_pack._DEFAULT_UNIT_CHARS)}{_TRAIL_LINK} shows.\n"
        },
        _TRAIL_PATH,
        id="unit-excerpt-and-block-body",
    ),
    pytest.param(
        {"bridge_lede": f"{_lead_in(context_pack._DEFAULT_CLAIM_CHARS)}{_TRAIL_LINK}\n\n"},
        _TRAIL_PATH,
        id="claim-lede",
    ),
    pytest.param(
        {
            "bridge_suffix": f"{_DECISION_HEAD}"
            f"- relations: evidenced_by: [[{_LONG_TRAIL_PATH.removesuffix('.md')}]]\n\n"
            "Cap the evening load.\n",
        },
        _LONG_TRAIL_PATH,
        id="unit-relation-target",
    ),
]


@pytest.mark.parametrize(("fixture", "source_path"), _CUT_FIELD_UNDER_STRIP)
def test_a_pack_field_that_a_length_cut_would_end_inside_a_source_link_is_withheld(
    vault: Path, fixture: dict[str, str], source_path: str
) -> None:
    # The page names the source nowhere but in the field under test.
    _write_bridge_fixture(
        vault,
        approval=True,
        source_path=source_path,
        **{
            "bridge_sources": "  - 'a note'\n",
            "bridge_relations": "No further relations.\n",
            **fixture,
        },
    )

    _assert_stem_absent(_ask_as_guest(vault))


def test_a_deep_pack_in_an_ungoverned_vault_keeps_the_source_trail_and_trims_a_long_field(
    vault: Path,
) -> None:
    (vault / BRIDGE_PATH).parent.mkdir(parents=True, exist_ok=True)
    (vault / BRIDGE_PATH).write_text(
        _bridge_text(source_path=_TRAIL_PATH, relations="No further relations.\n")
        + _TRAIL_DECISION.replace(
            f"Derived from {_TRAIL_LINK} in full.",
            f"{_lead_in(context_pack._MAX_UNIT_CONTEXT_CHARS)}{_TRAIL_LINK}",
        ),
        encoding="utf-8",
    )
    find_module.clear_cache()

    with request_scope(_external()):
        found = commands.op_ask_memory(
            vault, query="unlimited evening capacity", mode="keyword", limit=10, deep=True
        )

    entry = found["pack"]["semantic_units"][BRIDGE_PATH]
    decision = entry["units"][1]
    assert entry["parent"]["sources"] == [_TRAIL_LINK]
    assert decision["relations"][0]["target"] == _TRAIL_LINK
    assert decision["context"].endswith("zqxv-l…")
    assert _TRAIL_LINK in decision["excerpt"]


def _publish_graph(vault: Path) -> None:
    """Counterpart discovery reads the published graph, so publish it as a running service would."""
    freshness.seed(
        vault, "vault", ((str(p), freshness.stat_signature(p)) for p in vault_module.walk_vault_md(vault))
    )
    freshness.seed(
        vault,
        "kb",
        ((str(p), freshness.stat_signature(p)) for p in find_module._walk_md(vault / "Knowledge Base")),
    )
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    find_module.evict_resolver_caches(vault)
    find_module.recall_resolver_snapshot(
        vault, expected_checkpoint=freshness.live_recall_checkpoint(vault, "vault")
    )


def test_a_correction_counterpart_on_a_page_under_a_release_strip_carries_no_source_trail(
    vault: Path,
) -> None:
    # The decision unit supports the capacity unit that the query selects, so the pack admits it as a
    # correction counterpart on the same page. Its fields are long enough for a length cut to end
    # inside the link, and so is the page's own `sources` entry.
    _write_bridge_fixture(
        vault,
        approval=True,
        source_path=_TRAIL_PATH,
        bridge_sources=(
            f"  - '{_lead_in(context_pack._MAX_PARENT_PROVENANCE_CHARS)}{_TRAIL_LINK}'\n"
        ),
        bridge_relations="No further relations.\n",
        bridge_suffix=f"{_DECISION_HEAD}- relations: supports: [[{BRIDGE_PATH.removesuffix('.md')}#capacity]]\n\n"
        f"{_lead_in(context_pack._DEFAULT_UNIT_CHARS)}{_TRAIL_LINK} shows.\n",
    )
    _publish_graph(vault)

    found = _ask_as_guest(vault, result_level="unit")

    pack = found["pack"]
    units = pack["semantic_units"][BRIDGE_PATH]["units"]
    assert pack["correction_context"] == "available"
    assert [unit["relation"] for unit in units if "corrects" in unit] == ["supports"]
    _assert_stem_absent(found)


def test_a_page_that_hosts_a_correction_counterpart_shows_no_link_to_a_withheld_page(
    vault: Path,
) -> None:
    # No bridge is involved: an ordinary page lists a source that the guest may not see, and its
    # decision unit supports its own observation, so the pack admits a counterpart on the same page.
    ordinary = "Knowledge Base/Notes/Insights/ordinary-capacity.md"
    _write_bridge_fixture(vault, approval=False, source_path=_TRAIL_PATH)
    (vault / ordinary).write_text(
        "---\ntype: insight\nexomem_id: 00000000-0000-4000-8000-000000000401\n"
        "title: Ordinary capacity\nstatus: active\ncreated: 2026-07-28\nupdated: 2026-07-28\n"
        f"sources:\n  - '{_TRAIL_LINK}'\ntags: [planning]\n---\n\n"
        "# Ordinary capacity\n\n"
        "## Observations\n"
        "- [constraint] Do not assume unlimited evening capacity #planning ^capacity\n"
        f"{_DECISION_HEAD}- relations: supports: [[{ordinary.removesuffix('.md')}#capacity]]\n\n"
        "Cap the evening load.\n",
        encoding="utf-8",
    )
    _publish_graph(vault)

    found = _ask_as_guest(vault, result_level="unit")

    pack = found["pack"]
    units = pack["semantic_units"][ordinary]["units"]
    assert [unit["relation"] for unit in units if "corrects" in unit] == ["supports"]
    _assert_stem_absent(found)


@pytest.mark.parametrize(
    ("source_path", "lede"),
    [
        pytest.param(
            f"{_TRAIL_DIRECTORY}zqxv. lab panel.md",
            f"Cap the load, as [[{_TRAIL_DIRECTORY}zqxv. lab panel]] shows.\n\n",
            id="sentence-split-inside-the-link",
        ),
        pytest.param(
            _TRAIL_PATH,
            f"{_lead_in(context_pack._NEIGHBOR_LEDE_CHARS)}{_TRAIL_LINK}\n\n",
            id="length-cut-inside-the-link",
        ),
    ],
)
def test_the_lede_of_a_neighbour_under_a_release_strip_is_withheld_when_it_would_be_cut(
    vault: Path, source_path: str, lede: str
) -> None:
    _write_bridge_fixture(
        vault,
        approval=True,
        source_path=source_path,
        bridge_sources="  - 'a note'\n",
        bridge_relations="No further relations.\n",
        bridge_lede=lede,
    )
    # The query finds only this page, which links to the bridge, so the bridge is its neighbour.
    (vault / "Knowledge Base/Notes/Insights/quasar-rota.md").write_text(
        "---\ntype: insight\nexomem_id: 00000000-0000-4000-8000-000000000301\n"
        "title: Quasar rota\nstatus: active\ncreated: 2026-07-28\nupdated: 2026-07-28\n---\n\n"
        f"# Quasar rota\n\nThe quasar rota follows [[{BRIDGE_PATH.removesuffix('.md')}]].\n",
        encoding="utf-8",
    )
    find_module.clear_cache()

    found = _ask_as_guest(vault, query="quasar rota")

    assert [entry["path"] for entry in found["pack"]["neighborhood"]] == [BRIDGE_PATH]
    _assert_stem_absent(found)


@pytest.mark.parametrize("separator", ["  ", "\u00a0", "\t"], ids=["double-space", "nbsp", "tab"])
def test_a_source_named_with_unusual_whitespace_is_stripped_from_the_collapsed_claim_text(
    vault: Path, separator: str
) -> None:
    # The pack collapses every whitespace run to one space before it strips, so the stripped
    # identity must match the collapsed spelling of the name.
    source_path = f"{_TRAIL_DIRECTORY}zqxv{separator}lab panel.md"
    link = f"[[{source_path.removesuffix('.md')}]]"
    _write_bridge_fixture(
        vault,
        approval=True,
        source_path=source_path,
        bridge_sources="  - 'a note'\n",
        bridge_relations="No further relations.\n",
        bridge_lede=f"Cap the load, as {link} shows.\n\n",
        bridge_suffix=f"{_DECISION_HEAD}- note: Derived from {link}.\n\nCap the evening load.\n",
    )

    found = _ask_as_guest(vault)

    assert found["pack"]["claims"][BRIDGE_PATH]["title"] == "Workload constraint"
    _assert_stem_absent(found)


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


def test_restriction_signature_moves_when_a_scope_declares_default_deny() -> None:
    """An approved bridge stales when the restriction behind its source moves.

    Differential against the equivalent authored `ceiling: 0`: declaring
    `default_deny` on the source scope closes it to every audience no rule
    names, so it must stale the approved abstraction exactly as writing the
    ceiling-0 rule does. Blind to the declaration, a previously approved
    abstraction of a private source keeps flowing after the owner locks the
    scope.
    """

    def signature(*, default_deny: bool = False, ceiling: int | None = None) -> str:
        compiled = policy.Policy(
            fingerprint="test",
            scopes={
                SCOPE_A: policy.Scope(
                    id=SCOPE_A, source="scopes/private.yaml", default_deny=default_deny
                )
            },
            rules=()
            if ceiling is None
            else (
                policy.Rule(
                    id=RULE_ID,
                    source="rules/private.yaml",
                    scope_ids=(SCOPE_A,),
                    audience="external",
                    ceiling=ceiling,
                ),
            ),
        )
        return bridges.restriction_signature(
            {SCOPE_A}, policy=compiled, audience="external"
        )

    open_scope = signature()
    # The control: authoring the equivalent restriction as a rule moves it.
    assert signature(ceiling=0) != open_scope
    # The declaration owes the same.
    assert signature(default_deny=True) != open_scope
    # ...and REMOVING it moves the signature back, so an owner who unlocks the
    # scope cannot leave a stale approval looking fresh either.
    assert signature(default_deny=False) == open_scope


def test_restriction_signature_ignores_a_declaration_on_an_unrelated_scope() -> None:
    """Only the source's own scopes bind the signature; a declaration on a
    scope the source is not a member of must not churn every approval."""
    def signature(*, default_deny: bool) -> str:
        compiled = policy.Policy(
            fingerprint="test",
            scopes={
                SCOPE_A: policy.Scope(id=SCOPE_A, source="scopes/private.yaml"),
                SCOPE_B: policy.Scope(
                    id=SCOPE_B, source="scopes/other.yaml", default_deny=default_deny
                ),
            },
        )
        return bridges.restriction_signature(
            {SCOPE_A}, policy=compiled, audience="external"
        )

    assert signature(default_deny=True) == signature(default_deny=False)


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


def test_unsafe_loaded_rule_options_block_all_content_paths_without_leaking(
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
    assert compiled.blocked
    assert compiled.rules == ()
    assert compiled.release_grants == ()
    assert {
        "rules/private.yaml:options.binary",
        "rules/private.yaml:options.day",
        "rules/private.yaml:options.instant",
        "rules/private.yaml:options.members",
        "rules/private.yaml:options.not_a_number",
        "rules/private.yaml:options.infinity",
    } <= {
        finding["path"]
        for finding in compiled.findings
        if finding["severity"] == "error"
    }

    with request_scope(_external()):
        with pytest.raises(ValueError, match="^NOT_FOUND") as get_error:
            commands.op_get(vault, path=BRIDGE_PATH)
        found = commands.op_find(
            vault,
            query="unlimited evening capacity",
            mode="keyword",
            graph=False,
            limit=10,
        )
        attention_result = egress.postfilter(
            "attention",
            attention.attention(
                vault, today=dt.date(2026, 7, 28), limit=0
            ).as_dict(),
            vault,
        )

    assert found == []
    assert attention_result["items"] == []
    assert str(get_error.value) == f"NOT_FOUND: file does not exist: {BRIDGE_PATH}"
    assert BRIDGE_PATH not in repr({"find": found, "attention": attention_result})
    projected = repr(
        {
            "get_error": str(get_error.value),
            "find": found,
            "attention": attention_result,
        }
    )
    for private_value in (
        SOURCE_PATH,
        "RESTRICTED-SENTINEL",
        "Private source title",
        "unlimited evening capacity",
    ):
        assert private_value not in projected


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

    page_wire = json.dumps(page, sort_keys=True, default=str).casefold()
    for wire in (page_wire, json.dumps(hits, sort_keys=True, default=str).casefold()):
        for alias in aliases:
            assert alias.casefold() not in wire
        decoded = wire
        for _ in range(8):
            decoded = unquote(decoded)
        assert not re.search(r"(?<!\w)apollo(?!\w)", decoded, re.IGNORECASE)
    # The search excerpt is cut from the page, so the strip withholds it; the read body keeps the text.
    assert "apollo%58" in page_wire
    assert "apollox" in page_wire


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
                "context": "ApolloX is unrelated.",
            },
            {"source_title": "Apollo"},
        ]
    }

    cleaned = bridges.strip_provenance(payload, (identity,))

    assert cleaned["relations"] == [
        {
            "source_title": "ApolloX",
            "candidate_id": "ApolloX",
            "context": "ApolloX is unrelated.",
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

    page_wire = json.dumps(page, sort_keys=True, default=str).casefold()
    for wire in (page_wire, json.dumps(hits, sort_keys=True, default=str).casefold()):
        decoded = wire
        for _ in range(8):
            decoded = unquote(decoded)
        assert not re.search(r"(?<!\w)apollo(?!\w)", decoded, re.IGNORECASE)
    # The search excerpt is cut from the page, so the strip withholds it; the read body keeps the text.
    assert "unrelated text remains" in page_wire
    assert "%ff" in page_wire


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
    payload = {"entries": [{"context": "ordinary prose"} for _ in range(30)]}
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
