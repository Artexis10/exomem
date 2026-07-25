"""Release plane: the per-level projector and decision annotation in `op_find`.

Design decisions D2 (decisions computed strictly after `find()` returns, cache
stays principal-free), D3 (the per-level projector is the ONLY serializer to a
wire dict), and D4 (request-deterministic backfill; L0 silent at exhaustion).

The load-bearing negative: a `blocked` policy — a cold-start compile refusal
with no prior good state — must fail closed to the most-restrictive outcome at
every consumer, and must never be conflated with an `empty` policy's open fast
path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import commands
from exomem import find as find_module
from exomem.find_types import GraphProvenance, Hit
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal, request_scope

# --------------------------------------------------------------------------
# Governed-vault helpers
# --------------------------------------------------------------------------

SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
GRANT_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB1"

PATTERNS_GLOB = "Notes/Patterns/**"
RESTRICTED_PATH = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
OPEN_PATH = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization.md"

EXTERNAL = "external"


def _gov_dir(vault: Path) -> Path:
    return vault / "Knowledge Base" / "_Governance"


def write_scope(vault: Path, *, paths: str = PATTERNS_GLOB, name: str = "Patterns") -> None:
    target = _gov_dir(vault) / "scopes" / "patterns.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: {name}\npaths: [\"{paths}\"]\n",
        encoding="utf-8",
    )


def write_rule(
    vault: Path,
    *,
    ceiling: int,
    audience: str = EXTERNAL,
    extra: str = "",
) -> None:
    target = _gov_dir(vault) / "rules" / "patterns-external.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"governance_version: 1\nid: {RULE_ID}\nscope_ids: [\"{SCOPE_ID}\"]\n"
        f"audience: {audience}\nceiling: {ceiling}\n{extra}",
        encoding="utf-8",
    )


def write_broken_policy(vault: Path) -> None:
    """A cold-start compile refusal: an out-of-range ceiling, no prior good
    compile for this vault -> `policy.load()` returns the BLOCKED policy."""
    write_scope(vault)
    target = _gov_dir(vault) / "rules" / "patterns-external.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"governance_version: 1\nid: {RULE_ID}\nscope_ids: [\"{SCOPE_ID}\"]\n"
        f"audience: {EXTERNAL}\nceiling: 9\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _clear_governance_caches():
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()
    yield
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()


def _reset_governance_caches() -> None:
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _hit(path: str, *, title: str = "t", excerpt: str = "e") -> Hit:
    return Hit(
        path=path,
        type="pattern",
        scope="proj",
        title=title,
        updated="2026-01-01",
        excerpt=excerpt,
        bm25_rank=1,
        vector_score=0.5,
    )


def _external(purpose: str | None = None) -> RequestPrincipal:
    return RequestPrincipal(audience_id=EXTERNAL, surface="mcp", purpose=purpose)


def _reset_caches() -> None:
    """Policy/membership/decision caches, between two differently-scoped calls."""
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()


# --------------------------------------------------------------------------
# D3 — the projector is the only serializer, with per-level allow-lists
# --------------------------------------------------------------------------


def test_project_l0_omits_the_item_entirely() -> None:
    assert egress.project(_hit(RESTRICTED_PATH), egress.LEVEL_NONE) is None


def test_project_l1_carries_only_rule_id_and_scope_label() -> None:
    out = egress.project(
        _hit(RESTRICTED_PATH),
        egress.LEVEL_NOTICE,
        rule_ids=(RULE_ID,),
        scope_label="Patterns",
    )
    assert out is not None
    assert out["withheld"] is True
    assert out["rule_ids"] == [RULE_ID]
    assert out["scope_label"] == "Patterns"
    # No metadata oracle of any kind may ride along at L1.
    for forbidden in (
        "path",
        "title",
        "excerpt",
        "signals",
        "graph",
        "relation_match",
        "matched_units",
        "superseded_by",
        "parent_ref",
        "scope",
        "type",
        "updated",
    ):
        assert forbidden not in out, f"L1 leaked {forbidden!r}"


def test_project_l2_adds_only_the_constraint_string() -> None:
    out = egress.project(
        _hit(RESTRICTED_PATH),
        egress.LEVEL_CONSTRAINT,
        rule_ids=(RULE_ID,),
        options={"constraint": "NDA until 2027", "abstract": "must not appear"},
    )
    assert out is not None
    assert out["constraint"] == "NDA until 2027"
    assert "abstract" not in out
    assert "path" not in out and "excerpt" not in out


def test_project_l3_adds_the_approved_abstract() -> None:
    out = egress.project(
        _hit(RESTRICTED_PATH),
        egress.LEVEL_ABSTRACT,
        options={"abstract": "a release-safety pattern"},
    )
    assert out is not None
    assert out["abstract"] == "a release-safety pattern"
    assert "path" not in out and "excerpt" not in out


def test_project_l4_falls_back_to_l3_abstract() -> None:
    """L4 redaction span maps are explicitly a later change; until then L4
    renders as L3 rather than leaking an unredacted excerpt."""
    l4 = egress.project(
        _hit(RESTRICTED_PATH),
        egress.LEVEL_EXCERPT_REDACTED,
        options={"abstract": "a release-safety pattern"},
    )
    l3 = egress.project(
        _hit(RESTRICTED_PATH),
        egress.LEVEL_ABSTRACT,
        options={"abstract": "a release-safety pattern"},
    )
    assert l4 == l3
    assert l4 is not None and "excerpt" not in l4


def test_project_l5_carries_path_title_and_excerpt() -> None:
    out = egress.project(_hit(RESTRICTED_PATH, title="Kill switch"), egress.LEVEL_EXCERPT)
    assert out is not None
    assert out["path"] == RESTRICTED_PATH
    assert out["title"] == "Kill switch"
    assert out["excerpt"] == "e"


def test_scores_and_signals_only_at_l5_and_above() -> None:
    for level in (
        egress.LEVEL_NOTICE,
        egress.LEVEL_CONSTRAINT,
        egress.LEVEL_ABSTRACT,
        egress.LEVEL_EXCERPT_REDACTED,
    ):
        out = egress.project(_hit(RESTRICTED_PATH), level)
        assert out is not None
        assert "signals" not in out, f"level {level} leaked ranking signals"
    for level in (egress.LEVEL_EXCERPT, egress.LEVEL_FULL):
        out = egress.project(_hit(RESTRICTED_PATH), level)
        assert out is not None
        assert "signals" in out and out["signals"]["bm25_rank"] == 1


def test_graph_seed_naming_a_withheld_path_is_stripped_at_every_level() -> None:
    hit = _hit(OPEN_PATH)
    hit.graph_provenance = GraphProvenance(
        relation_type="supports", direction="outbound", seed=RESTRICTED_PATH
    )
    for level in (egress.LEVEL_EXCERPT, egress.LEVEL_FULL):
        out = egress.project(hit, level, withheld_paths=frozenset({RESTRICTED_PATH}))
        assert out is not None
        assert "graph" not in out, f"level {level} leaked a withheld graph seed"
        # The hit itself is still released — only the provenance is stripped.
        assert out["path"] == OPEN_PATH


def test_graph_seed_naming_a_permitted_path_survives_at_l5() -> None:
    hit = _hit(OPEN_PATH)
    hit.graph_provenance = GraphProvenance(
        relation_type="supports", direction="outbound", seed=OPEN_PATH
    )
    out = egress.project(hit, egress.LEVEL_EXCERPT, withheld_paths=frozenset())
    assert out is not None
    assert out["graph"]["seed"] == OPEN_PATH


def test_relation_match_naming_a_withheld_path_is_stripped() -> None:
    hit = _hit(OPEN_PATH)
    hit.relation_match = {"relation": "supports", "target": RESTRICTED_PATH}
    out = egress.project(hit, egress.LEVEL_FULL, withheld_paths=frozenset({RESTRICTED_PATH}))
    assert out is not None
    assert "relation_match" not in out


def test_superseded_by_naming_a_withheld_path_is_stripped() -> None:
    hit = _hit(OPEN_PATH)
    hit.superseded_by = [RESTRICTED_PATH]
    out = egress.project(hit, egress.LEVEL_FULL, withheld_paths=frozenset({RESTRICTED_PATH}))
    assert out is not None
    assert "superseded_by" not in out


# --------------------------------------------------------------------------
# M14 — a withheld path is withheld in every form it can be written
# --------------------------------------------------------------------------

WITHHELD = frozenset({RESTRICTED_PATH})
RESTRICTED_STEM = "kill-switch-for-risky-releases"
RESTRICTED_KB_RELATIVE = "Notes/Patterns/kill-switch-for-risky-releases.md"


@pytest.mark.parametrize(
    ("form", "value"),
    [
        ("exact", RESTRICTED_PATH),
        ("heading_anchor", f"{RESTRICTED_PATH}#Kill switch semantics"),
        ("no_extension", RESTRICTED_PATH[: -len(".md")]),
        ("kb_relative", RESTRICTED_KB_RELATIVE),
        ("kb_relative_anchor", f"{RESTRICTED_KB_RELATIVE}#Rollback"),
        ("wikilink", f"[[{RESTRICTED_STEM}]]"),
        ("wikilink_alias", f"[[{RESTRICTED_STEM}|the kill switch]]"),
        ("wikilink_anchor", f"[[{RESTRICTED_STEM}#Rollback]]"),
        ("wikilink_full_path", f"[[{RESTRICTED_PATH}]]"),
        ("bare_filename", f"{RESTRICTED_STEM}.md"),
        ("exomem_vault_ref", f"exomem://vault/{RESTRICTED_PATH.replace(' ', '%20')}"),
        (
            "exomem_source_ref",
            f"exomem://source/{RESTRICTED_PATH[: -len('.md')].replace(' ', '%20')}",
        ),
        ("windows_separators", RESTRICTED_PATH.replace("/", "\\")),
        ("leading_slash", f"/{RESTRICTED_PATH}"),
    ],
)
def test_a_withheld_path_is_recognised_in_every_reference_form(
    form: str, value: str
) -> None:
    """M14: `_names_withheld` compared raw strings, so a withheld page
    referenced with an anchor, as a wikilink, or in `exomem://` citation form
    survived in a released payload — leaving a permitted page as an existence
    oracle for its withheld neighbour."""
    assert egress._names_withheld(value, WITHHELD), form


def test_a_permitted_sibling_with_the_same_stem_survives() -> None:
    """The normalization must not become a basename blocklist: a DIFFERENT
    page that happens to share a filename, referenced by its own full path,
    is not the withheld one."""
    other = "Knowledge Base/Sources/kill-switch-for-risky-releases.md"
    assert not egress._names_withheld(other, WITHHELD)
    assert not egress._names_withheld(f"{other}#Intro", WITHHELD)


def test_ordinary_prose_and_unrelated_paths_are_not_withheld() -> None:
    assert not egress._names_withheld(OPEN_PATH, WITHHELD)
    assert not egress._names_withheld("a note about kill switches", WITHHELD)
    assert not egress._names_withheld("[[rrf-fusion-beats-score-normalization]]", WITHHELD)


def test_a_bare_word_matching_a_withheld_stem_is_not_treated_as_a_reference() -> None:
    """The other half of the asymmetry: stem comparison applies only to
    strings that are unambiguously references (wikilink-wrapped,
    `exomem://`-prefixed, or `.md`-suffixed). A plain title that happens to
    equal a withheld page's filename is not a reference to it, and stripping
    it would degrade permitted output for nothing."""
    withheld_overview = frozenset({"Knowledge Base/Notes/Patterns/overview.md"})
    assert not egress._names_withheld("Overview", withheld_overview)
    assert not egress._names_withheld("overview", withheld_overview)
    # …but the reference forms of that same page still match.
    assert egress._names_withheld("[[overview]]", withheld_overview)
    assert egress._names_withheld("overview.md", withheld_overview)


def test_provenance_fields_are_stripped_for_every_reference_form() -> None:
    """End-to-end through the projector: the annotation shapes that carry
    pointers must drop the withheld neighbour however it was written."""
    hit = _hit(OPEN_PATH)
    hit.superseded_by = [f"[[{RESTRICTED_STEM}]]"]
    hit.relation_match = {
        "relation": "supports",
        "target": f"exomem://source/{RESTRICTED_PATH[: -len('.md')].replace(' ', '%20')}",
    }
    hit.matched_units = [{"unit_ref": "u1", "parent_path": f"{RESTRICTED_PATH}#Rollback"}]
    out = egress.project(hit, egress.LEVEL_FULL, withheld_paths=WITHHELD)
    assert out is not None
    assert "superseded_by" not in out
    assert "relation_match" not in out
    assert "matched_units" not in out


def test_matched_units_naming_a_withheld_parent_are_stripped() -> None:
    hit = _hit(OPEN_PATH)
    hit.matched_units = [{"unit_ref": "u1", "parent_path": RESTRICTED_PATH}]
    out = egress.project(hit, egress.LEVEL_FULL, withheld_paths=frozenset({RESTRICTED_PATH}))
    assert out is not None
    assert "matched_units" not in out


def test_unregistered_payload_fails_closed() -> None:
    """A content-returning surface with no registered projector emits no path,
    title, or excerpt — the omission is this assertion, not a silent leak."""

    class UnknownPayload:
        path = RESTRICTED_PATH
        title = "secret"
        excerpt = "secret body"

    out = egress.project(UnknownPayload(), egress.LEVEL_FULL)
    assert out is not None
    assert out["withheld"] is True
    assert out.get("reason") == "no_projector"
    for forbidden in ("path", "title", "excerpt"):
        assert forbidden not in out


# --------------------------------------------------------------------------
# D2 — annotate_hits, and the empty / blocked / governed three-way split
# --------------------------------------------------------------------------


def test_annotate_hits_empty_policy_is_the_open_fast_path(vault: Path) -> None:
    hits = [_hit(RESTRICTED_PATH), _hit(OPEN_PATH)]
    result = egress.annotate_hits(vault, hits, principal=_external(), limit=15)
    assert result.active is False
    assert result.hits == hits
    assert result.notices == []
    assert result.withheld_paths == frozenset()
    # Nothing principal-dependent was written onto the candidates.
    assert all(h.decision is None for h in result.hits)


def test_annotate_hits_blocked_policy_withholds_everything(vault: Path) -> None:
    """THE fail-closed contract: a refused cold-start compile is not `empty`.
    Nothing may leak, and the open fast path must not be reachable."""
    write_broken_policy(vault)
    hits = [_hit(RESTRICTED_PATH), _hit(OPEN_PATH)]
    result = egress.annotate_hits(vault, hits, principal=_external(), limit=15)
    assert result.active is True
    assert result.blocked is True
    assert result.hits == []
    # L0/DISCLOSURE_MIN semantics: silent, so not even a notice count leaks.
    assert result.notices == []
    assert result.withheld_paths == frozenset({RESTRICTED_PATH, OPEN_PATH})


def test_annotate_hits_blocked_policy_withholds_for_the_owner_too(vault: Path) -> None:
    """`blocked` is a state of the policy, not of the audience — it cannot be
    escaped by being the owner."""
    write_broken_policy(vault)
    owner = RequestPrincipal(audience_id="owner", surface="cli")
    result = egress.annotate_hits(vault, [_hit(OPEN_PATH)], principal=owner, limit=15)
    assert result.hits == []
    assert result.blocked is True


def test_annotate_hits_unresolved_principal_fails_closed(vault: Path) -> None:
    """A surface that expected an identity and could not resolve one must not
    be handed the open fast path even on a governed vault."""
    from exomem.governance.principal import most_restrictive_principal

    write_scope(vault)
    write_rule(vault, ceiling=0)
    result = egress.annotate_hits(
        vault,
        [_hit(RESTRICTED_PATH), _hit(OPEN_PATH)],
        principal=most_restrictive_principal(surface="rest"),
        limit=15,
    )
    assert result.hits == []
    assert result.withheld_paths == frozenset({RESTRICTED_PATH, OPEN_PATH})


def test_annotate_hits_withholds_below_excerpt_and_keeps_permitted(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    hits = [_hit(RESTRICTED_PATH), _hit(OPEN_PATH)]
    result = egress.annotate_hits(vault, hits, principal=_external(), limit=15)
    assert [h.path for h in result.hits] == [OPEN_PATH]
    assert result.withheld_paths == frozenset({RESTRICTED_PATH})
    assert result.hits[0].decision is not None
    assert result.hits[0].decision.level == egress.LEVEL_FULL


def test_annotate_hits_l1_scope_emits_a_notice_at_pool_exhaustion(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=1)
    hits = [_hit(RESTRICTED_PATH), _hit(OPEN_PATH)]
    # limit 2, pool holds 2 -> nothing left to backfill with -> notice shows.
    result = egress.annotate_hits(vault, hits, principal=_external(), limit=2)
    assert [h.path for h in result.hits] == [OPEN_PATH]
    assert len(result.notices) == 1
    notice = result.notices[0]
    assert notice["withheld"] is True
    assert notice["rule_ids"] == [RULE_ID]
    assert "path" not in notice and "excerpt" not in notice


def test_annotate_hits_l0_scope_is_silent_at_pool_exhaustion(vault: Path) -> None:
    """A fixed 'governance active' marker for an L0 scope would itself be an
    existence oracle — L0 returns a silently shorter list instead."""
    write_scope(vault)
    write_rule(vault, ceiling=0)
    result = egress.annotate_hits(
        vault, [_hit(RESTRICTED_PATH), _hit(OPEN_PATH)], principal=_external(), limit=2
    )
    assert result.notices == []
    assert len(result.hits) == 1


def test_annotate_hits_backfills_from_the_over_fetch_pool(vault: Path) -> None:
    """The shown count is a function of the request, not of how many
    candidates were withheld."""
    write_scope(vault)
    write_rule(vault, ceiling=0)
    pool = [
        _hit(RESTRICTED_PATH),
        _hit(OPEN_PATH),
        _hit("Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout.md"),
    ]
    result = egress.annotate_hits(vault, pool, principal=_external(), limit=2)
    assert len(result.hits) == 2
    assert RESTRICTED_PATH not in [h.path for h in result.hits]
    assert result.notices == []


def test_annotate_hits_owner_sees_everything(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0, audience=EXTERNAL)
    owner = RequestPrincipal(audience_id="owner", surface="cli")
    result = egress.annotate_hits(
        vault, [_hit(RESTRICTED_PATH), _hit(OPEN_PATH)], principal=owner, limit=15
    )
    assert len(result.hits) == 2


def test_annotate_hits_reads_purpose_off_the_bound_principal(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    with request_scope(_external(purpose="audit")):
        result = egress.annotate_hits(vault, [_hit(RESTRICTED_PATH)], limit=15)
    assert result.active is True


def test_annotate_hits_uses_the_bound_principal_when_none_passed(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    with request_scope(_external()):
        result = egress.annotate_hits(vault, [_hit(RESTRICTED_PATH)], limit=15)
    assert result.hits == []


# --------------------------------------------------------------------------
# D2 — op_find integration: annotation strictly after find(), before pack
# --------------------------------------------------------------------------


def test_op_find_annotates_and_filters(vault: Path) -> None:
    """Withheld hit -> dropped/notice; permitted hit -> released; the pack
    never contains a withheld item."""
    write_scope(vault)
    write_rule(vault, ceiling=0)
    with request_scope(_external()):
        result = commands.op_find(vault, query="kill switch risky releases", limit=10, pack=True)
    hits = result["hits"] if isinstance(result, dict) else result
    paths = [h.get("path") for h in hits if "path" in h]
    assert RESTRICTED_PATH not in paths
    pack = result["pack"] if isinstance(result, dict) else None
    if pack is not None:
        assert RESTRICTED_PATH not in pack.get("packed_paths", [])


def test_op_find_owner_is_unchanged(vault: Path) -> None:
    """Baseline preservation: an ungoverned vault under the owner returns the
    same paths with and without the release plane in the call path."""
    baseline = commands.op_find(vault, query="kill switch risky releases", limit=10)
    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        governed = commands.op_find(vault, query="kill switch risky releases", limit=10)
    assert [h["path"] for h in baseline] == [h["path"] for h in governed]


def test_op_find_blocked_policy_returns_nothing(vault: Path) -> None:
    write_broken_policy(vault)
    with request_scope(_external()):
        result = commands.op_find(vault, query="kill switch risky releases", limit=10)
    hits = result["hits"] if isinstance(result, dict) else result
    assert hits == []


def test_op_find_never_leaks_a_withheld_path_in_graph_provenance(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    with request_scope(_external()):
        result = commands.op_find(vault, query="retry backoff jitter", limit=10, graph=True)
    hits = result["hits"] if isinstance(result, dict) else result
    for hit in hits:
        seed = (hit.get("graph") or {}).get("seed")
        assert seed != RESTRICTED_PATH
        assert RESTRICTED_PATH not in str(hit.get("relation_match") or "")
        assert RESTRICTED_PATH not in (hit.get("superseded_by") or [])


# --------------------------------------------------------------------------
# D2 / cache invariant — task 3.1
# --------------------------------------------------------------------------


def test_find_hot_cache_stays_principal_free(vault: Path) -> None:
    """Two audiences, second call served from the hot cache: decisions are
    recomputed per request and NO cached candidate copy carries a prior
    audience's decision."""
    write_scope(vault)
    write_rule(vault, ceiling=0)
    query = "kill switch risky releases"

    with request_scope(_external()):
        commands.op_find(vault, query=query, limit=10)
    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        second = commands.op_find(vault, query=query, limit=10)

    # The owner sees the restricted page the external audience could not.
    assert RESTRICTED_PATH in [h["path"] for h in second]

    # Every Hit sitting in the shared hot cache is principal-free.
    cached_hits = [
        hit for cached in find_module._FIND_CACHE.values() for hit in cached
    ]
    assert cached_hits, "expected the hot cache to be populated"
    assert all(getattr(hit, "decision", None) is None for hit in cached_hits)


def test_purpose_never_enters_the_find_cache_key(vault: Path) -> None:
    """A model-declared purpose must not be able to bust the relevance cache."""
    query = "kill switch risky releases"
    with request_scope(_external(purpose="audit")):
        commands.op_find(vault, query=query, limit=10)
    keys_after_first = set(find_module._FIND_CACHE.keys())
    with request_scope(_external(purpose="due-diligence")):
        commands.op_find(vault, query=query, limit=10)
    assert set(find_module._FIND_CACHE.keys()) == keys_after_first
    for key in keys_after_first:
        assert "audit" not in repr(key)
        assert "due-diligence" not in repr(key)


def test_decision_memo_is_keyed_on_audience_and_purpose(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    egress.clear_decision_memo()
    egress.annotate_hits(vault, [_hit(RESTRICTED_PATH)], principal=_external(), limit=5)
    first = egress.decision_memo_size()
    assert first >= 1
    # Same audience + purpose -> memo hit, no growth.
    egress.annotate_hits(vault, [_hit(RESTRICTED_PATH)], principal=_external(), limit=5)
    assert egress.decision_memo_size() == first
    # A different audience is a different memo entry.
    egress.annotate_hits(
        vault,
        [_hit(RESTRICTED_PATH)],
        principal=RequestPrincipal(audience_id="other", surface="mcp"),
        limit=5,
    )
    assert egress.decision_memo_size() > first


# --------------------------------------------------------------------------
# op_get — direct reads render at the release decision's level (task 4.1)
# --------------------------------------------------------------------------


def test_get_respects_decision_levels(vault: Path) -> None:
    """Full body at L6, a bounded excerpt at L5, an abstract at L3."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    with request_scope(_external()):
        full = commands.op_get(vault, path=RESTRICTED_PATH)
    assert "Kill switch" in full["body"]
    full_body = full["body"]

    write_rule(vault, ceiling=egress.LEVEL_EXCERPT)
    with request_scope(_external()):
        excerpt = commands.op_get(vault, path=RESTRICTED_PATH)
    assert excerpt["body"] != full_body
    assert len(excerpt["body"]) < len(full_body)
    assert excerpt.get("release_level") == egress.LEVEL_EXCERPT

    write_rule(vault, ceiling=egress.LEVEL_ABSTRACT, extra="options:\n  abstract: a safety pattern\n")
    with request_scope(_external()):
        abstract = commands.op_get(vault, path=RESTRICTED_PATH)
    assert abstract.get("abstract") == "a safety pattern"
    assert "body" not in abstract
    assert "frontmatter" not in abstract


def test_get_sub_notice_is_byte_identical_to_missing(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    with request_scope(_external()):
        with pytest.raises(ValueError) as withheld:
            commands.op_get(vault, path=RESTRICTED_PATH)
    with pytest.raises(ValueError) as missing:
        commands.op_get(vault, path="Knowledge Base/Notes/Patterns/no-such-page.md")
    # Same code, and the reason names the requested path in the same shape —
    # a withheld page must be indistinguishable from one that never existed.
    assert str(withheld.value).split(":")[0] == str(missing.value).split(":")[0] == "NOT_FOUND"
    assert str(withheld.value) == f"NOT_FOUND: file does not exist: {RESTRICTED_PATH}"


def test_get_blocked_policy_denies(vault: Path) -> None:
    """The `.blocked` fail-closed contract at the `op_get` consumer."""
    write_broken_policy(vault)
    with request_scope(_external()):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_get(vault, path=OPEN_PATH)


def test_get_ungoverned_page_is_unchanged(vault: Path) -> None:
    baseline = commands.op_get(vault, path=OPEN_PATH)
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    with request_scope(_external()):
        governed = commands.op_get(vault, path=OPEN_PATH)
    assert governed == baseline


def test_annotate_page_empty_policy_is_untouched(vault: Path) -> None:
    page = {"path": OPEN_PATH, "body": "hello", "frontmatter": {}}
    out = egress.annotate_page(vault, dict(page), principal=_external())
    assert out == page


def test_annotate_page_blocked_policy_returns_none(vault: Path) -> None:
    write_broken_policy(vault)
    out = egress.annotate_page(
        vault, {"path": OPEN_PATH, "body": "hello"}, principal=_external()
    )
    assert out is None


def test_annotate_page_strips_provenance_naming_a_withheld_item(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    page = {
        "path": OPEN_PATH,
        "body": "hello",
        "frontmatter": {"superseded_by": [RESTRICTED_PATH], "tags": ["x"]},
        "links": {"outbound": [RESTRICTED_PATH, OPEN_PATH]},
    }
    out = egress.annotate_page(vault, page, principal=_external())
    assert out is not None
    assert RESTRICTED_PATH not in str(out)


# --------------------------------------------------------------------------
# graph lane — guard_seed (task 4.1)
# --------------------------------------------------------------------------


def test_graph_context_guard_seed_governed(vault: Path) -> None:
    """Seeds, nodes, and edge endpoints naming a sub-notice page are dropped."""
    withheld = frozenset({RESTRICTED_PATH})
    payload = {
        "available": True,
        "seeds": [{"node_key": f"file:{RESTRICTED_PATH}", "path": RESTRICTED_PATH}],
        "nodes": [
            {"node_key": f"file:{RESTRICTED_PATH}", "path": RESTRICTED_PATH},
            {"node_key": f"file:{OPEN_PATH}", "path": OPEN_PATH},
        ],
        "edges": [
            {
                "edge_key": "e1",
                "src_key": f"file:{RESTRICTED_PATH}",
                "dst_key": f"file:{OPEN_PATH}",
            },
            {
                "edge_key": "e2",
                "src_key": f"file:{OPEN_PATH}",
                "dst_key": f"file:{OPEN_PATH}",
            },
        ],
    }
    out = egress.guard_seed(payload, withheld)
    assert out["seeds"] == []
    assert [n["path"] for n in out["nodes"]] == [OPEN_PATH]
    assert [e["edge_key"] for e in out["edges"]] == ["e2"]
    assert RESTRICTED_PATH not in str(out)


def test_guard_seed_no_withheld_paths_is_identity(vault: Path) -> None:
    payload = {
        "available": True,
        "seeds": [{"node_key": "a", "path": OPEN_PATH}],
        "nodes": [{"node_key": "a", "path": OPEN_PATH}],
        "edges": [{"edge_key": "e", "src_key": "a", "dst_key": "a"}],
    }
    assert egress.guard_seed(dict(payload), frozenset()) == payload


def test_op_graph_context_is_gated(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    with request_scope(_external()):
        out = commands.op_graph_context(vault, path=OPEN_PATH)
    assert RESTRICTED_PATH not in str(out)


def test_op_graph_context_blocked_policy_denies(vault: Path) -> None:
    """The `.blocked` fail-closed contract at the graph consumer."""
    write_broken_policy(vault)
    with request_scope(_external()):
        out = commands.op_graph_context(vault, path=OPEN_PATH)
    assert out.get("seeds") == []
    assert out.get("nodes") == []
    assert out.get("edges") == []


def test_graph_only_hit_from_a_withheld_seed_is_dropped(vault: Path) -> None:
    """D4: a hit whose ONLY provenance is expansion from a withheld seed is
    dropped; it returns only if it matched a lane on its own."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    smuggled = _hit(OPEN_PATH)
    smuggled.bm25_rank = None
    smuggled.vector_rank = None
    smuggled.vector_score = None
    smuggled.keyword_rank = None
    smuggled.graph_hop = True
    smuggled.graph_provenance = GraphProvenance(
        relation_type="supports", direction="outbound", seed=RESTRICTED_PATH
    )
    result = egress.annotate_hits(
        vault, [_hit(RESTRICTED_PATH), smuggled], principal=_external(), limit=10
    )
    assert result.hits == []


def test_graph_hit_that_also_matched_a_lane_survives(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    own_match = _hit(OPEN_PATH)
    own_match.graph_hop = True
    own_match.graph_provenance = GraphProvenance(
        relation_type="supports", direction="outbound", seed=RESTRICTED_PATH
    )
    result = egress.annotate_hits(
        vault, [_hit(RESTRICTED_PATH), own_match], principal=_external(), limit=10
    )
    assert [h.path for h in result.hits] == [OPEN_PATH]


# --------------------------------------------------------------------------
# packs carry decisions (task 4.1)
# --------------------------------------------------------------------------


def test_pack_header_carries_governance_context(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NOTICE)
    with request_scope(_external()):
        result = commands.op_find(
            vault, query="kill switch risky releases", limit=2, pack=True
        )
    pack = result["pack"]
    # The block appears because something WAS withheld; the fingerprint is
    # owner-only (see `test_pack_never_shows_the_policy_fingerprint_to_a_non_owner`).
    assert "governance" in pack
    assert "fingerprint" not in pack["governance"]
    assert RESTRICTED_PATH not in str(pack["packed_paths"])


def test_pack_excludes_withheld_neighborhood(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    with request_scope(_external()):
        result = commands.op_find(
            vault, query="kill switch risky releases", limit=5, pack=True
        )
    pack = result["pack"]
    for section in ("packed_paths", "claims", "neighborhood", "contradictions"):
        assert RESTRICTED_PATH not in str(pack.get(section, []))


# ==========================================================================
# Security review findings
# ==========================================================================


# --- C1: op_fetch is ungated -------------------------------------------


def test_fetch_respects_the_release_decision(vault: Path) -> None:
    """`op_fetch` is the ChatGPT deep-research read step. It called
    `get_page` directly, so an L0 item's full body, title, frontmatter and
    canonical path crossed the boundary to `audience=external`."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    with request_scope(_external()):
        with pytest.raises(ValueError) as err:
            commands.op_fetch(vault, id=RESTRICTED_PATH)
    assert str(err.value) == f"NOT_FOUND: file does not exist: {RESTRICTED_PATH}"


def test_fetch_sub_notice_is_byte_identical_to_missing(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    with request_scope(_external()):
        with pytest.raises(ValueError) as withheld:
            commands.op_fetch(vault, id=RESTRICTED_PATH)
    missing_path = "Knowledge Base/Notes/Patterns/no-such-page.md"
    with pytest.raises(ValueError) as missing:
        commands.op_fetch(vault, id=missing_path)
    assert str(withheld.value).split(":")[0] == str(missing.value).split(":")[0]


def test_fetch_blocked_policy_denies(vault: Path) -> None:
    write_broken_policy(vault)
    with request_scope(_external()):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_fetch(vault, id=OPEN_PATH)


def test_fetch_unresolved_principal_denies(vault: Path) -> None:
    from exomem.governance.principal import most_restrictive_principal

    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    with request_scope(most_restrictive_principal(surface="mcp")):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_fetch(vault, id=RESTRICTED_PATH)


def test_fetch_caps_text_to_an_excerpt_at_l5(vault: Path) -> None:
    """L5 permits a bounded excerpt, not the whole body `fetch` normally
    returns."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    with request_scope(_external()):
        full = commands.op_fetch(vault, id=RESTRICTED_PATH)
    write_rule(vault, ceiling=egress.LEVEL_EXCERPT)
    _reset_governance_caches()
    with request_scope(_external()):
        excerpt = commands.op_fetch(vault, id=RESTRICTED_PATH)
    assert len(excerpt["text"]) < len(full["text"])


def test_fetch_ungoverned_is_unchanged(vault: Path) -> None:
    baseline = commands.op_fetch(vault, id=OPEN_PATH)
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_governance_caches()
    with request_scope(_external()):
        governed = commands.op_fetch(vault, id=OPEN_PATH)
    assert governed == baseline


def test_fetch_is_a_registered_content_returning_command() -> None:
    registry = {c.name: c for c in commands.COMMANDS}
    derived = egress.content_returning_commands(registry)
    assert "fetch" in derived and "search" in derived
    assert egress._COMMAND_PROJECTOR_KIND["fetch"] == "page"


# --- C2: client-declared purpose defeats `outside` restrictions ---------


def test_declared_purpose_cannot_lift_an_outside_restriction(vault: Path) -> None:
    """The original escalation probe, now failing to escalate.

    `purpose_condition: outside` restricts unless the declared purpose
    matches — so declaring the magic value used to switch the restriction
    off. Purpose may now only narrow: the evaluator takes the minimum of the
    declared and undeclared branches, so `min(6, 0) = 0` and the restriction
    holds no matter what the client claims.
    """
    write_scope(vault)
    write_rule(
        vault, ceiling=0, extra="purpose: audit\npurpose_condition: outside\n"
    )
    undeclared = egress.annotate_hits(
        vault, [_hit(RESTRICTED_PATH)], principal=_external(), limit=5
    )
    assert undeclared.hits == []
    declared = egress.annotate_hits(
        vault, [_hit(RESTRICTED_PATH)], principal=_external(), purpose="audit", limit=5
    )
    assert declared.hits == [], (
        "a client-declared purpose lifted an `outside` restriction"
    )


def test_declared_purpose_cannot_widen_even_for_the_owner(vault: Path) -> None:
    """The invariant is a property of the evaluator, not of the caller — it
    holds for the owner too. Widening belongs to identity-conditioned grants."""
    write_scope(vault)
    write_rule(
        vault,
        ceiling=0,
        audience="owner",
        extra="purpose: audit\npurpose_condition: outside\n",
    )
    owner = RequestPrincipal(audience_id="owner", surface="cli")
    assert egress.annotate_hits(
        vault, [_hit(RESTRICTED_PATH)], principal=owner, limit=5
    ).hits == []
    assert egress.annotate_hits(
        vault, [_hit(RESTRICTED_PATH)], principal=owner, purpose="audit", limit=5
    ).hits == []


def test_declared_purpose_still_narrows(vault: Path) -> None:
    """The direction that remains: a purpose-conditioned rule that LOWERS the
    ceiling still applies when that purpose is declared."""
    write_scope(vault)
    write_rule(
        vault, ceiling=0, extra="purpose: marketing\npurpose_condition: matches\n"
    )
    assert [
        h.path
        for h in egress.annotate_hits(
            vault, [_hit(RESTRICTED_PATH)], principal=_external(), limit=5
        ).hits
    ] == [RESTRICTED_PATH]
    assert egress.annotate_hits(
        vault, [_hit(RESTRICTED_PATH)], principal=_external(), purpose="marketing", limit=5
    ).hits == []


def test_purpose_is_safe_on_the_wire(vault: Path) -> None:
    """Because a lying client can now only restrict itself, `purpose` stays
    on the MCP schema — the tool surface is unchanged by this property."""
    import json
    from pathlib import Path as _P

    schemas = json.loads(
        _P("tests/fixtures/mcp_tool_schemas.json").read_text(encoding="utf-8")
    )
    advertised = [
        name
        for name, entry in schemas.items()
        if "purpose" in ((entry.get("inputSchema") or {}).get("properties") or {})
    ]
    assert advertised, "expected `purpose` to remain on the recall leaves"


# --- C3 + H5: decision memo key omits page identity and vault_root ------


def test_revocation_takes_effect_within_the_process(vault: Path) -> None:
    """C3: retagging a note into a restricted scope was a no-op for any
    principal already served — the memo key omitted page identity, so the
    stale permissive decision was served for the process lifetime."""
    scope = _gov_dir(vault) / "scopes" / "tagged.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Secret\ntags: [\"secret\"]\n",
        encoding="utf-8",
    )
    write_rule(vault, ceiling=egress.LEVEL_NONE)

    rel = "Knowledge Base/Notes/Insights/revocation-probe.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: insight\ntitle: Probe\ntags: [ops]\n---\n\n# Probe\n",
        encoding="utf-8",
    )

    first = egress.annotate_hits(vault, [_hit(rel)], principal=_external(), limit=5)
    assert [h.path for h in first.hits] == [rel]

    # The OWNER retags it into the restricted scope. The policy has not
    # changed, so its fingerprint has not moved — only the page did.
    target.write_text(
        "---\ntype: insight\ntitle: Probe\ntags: [ops, secret]\n---\n\n# Probe\n",
        encoding="utf-8",
    )

    second = egress.annotate_hits(vault, [_hit(rel)], principal=_external(), limit=5)
    assert second.hits == [], "revocation never took effect: stale memo entry served"


def test_two_vaults_sharing_a_policy_do_not_share_decisions(
    vault: Path, tmp_path: Path
) -> None:
    """H5: `policy._content_fingerprint` hashes only relative path + bytes, so
    two vaults with identical `_Governance/` trees get identical fingerprints.
    Without `vault_root` in the memo key, vault A's restricted decision is
    served for vault B's same-named page (and vice-versa)."""
    # A TAG scope, so the same policy bytes produce DIFFERENT membership in
    # the two vaults — that difference is what the collision erases.
    scope = _gov_dir(vault) / "scopes" / "patterns.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Secret\ntags: [\"secret\"]\n",
        encoding="utf-8",
    )
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    target = vault / RESTRICTED_PATH
    target.write_text(
        "---\ntype: pattern\ntags: [secret]\n---\n\n# Vault A restricted page\n",
        encoding="utf-8",
    )

    other = tmp_path / "vault-b"
    (other / "Knowledge Base" / "Notes" / "Patterns").mkdir(parents=True)
    # Same path, NO `secret` tag -> not a scope member -> must stay permitted.
    (other / RESTRICTED_PATH).write_text(
        "---\ntype: pattern\ntitle: B\n---\n\n# Vault B page\n", encoding="utf-8"
    )
    # Same governance bytes -> same policy fingerprint.
    for name in ("scopes/patterns.yaml", "rules/patterns-external.yaml"):
        src = _gov_dir(vault) / name
        dst = _gov_dir(other) / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    from exomem.governance import policy as policy_module

    assert policy_module.load(vault).fingerprint == policy_module.load(other).fingerprint

    a = egress.annotate_hits(vault, [_hit(RESTRICTED_PATH)], principal=_external(), limit=5)
    b = egress.annotate_hits(other, [_hit(RESTRICTED_PATH)], principal=_external(), limit=5)
    assert a.hits == []
    assert [h.path for h in b.hits] == [RESTRICTED_PATH], (
        "vault B reused vault A's restricted decision under a shared fingerprint"
    )


def test_decision_memo_fails_closed_when_the_page_cannot_be_stat_ed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    real_stat = Path.stat

    def _boom(self, *a, **kw):
        if str(self).endswith("kill-switch-for-risky-releases.md"):
            raise OSError("stat refused")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", _boom)
    result = egress.annotate_hits(
        vault, [_hit(RESTRICTED_PATH)], principal=_external(), limit=5
    )
    assert result.hits == []


# --- C4: structure / review surfaces are an existence oracle ------------


def _restricted_vault(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)


def test_filter_withheld_entries_drops_path_bearing_entries(vault: Path) -> None:
    _restricted_vault(vault)
    payload = {
        "entries": [
            {"path": RESTRICTED_PATH, "size": 1234, "mtime": 1.0, "type": "pattern"},
            {"path": OPEN_PATH, "size": 10, "mtime": 2.0, "type": "insight"},
        ],
        "count": 2,
    }
    out = egress.filter_withheld_entries(vault, payload, principal=_external())
    assert [e["path"] for e in out["entries"]] == [OPEN_PATH]
    assert RESTRICTED_PATH not in str(out)


def test_filter_withheld_entries_drops_bare_path_strings(vault: Path) -> None:
    _restricted_vault(vault)
    payload = {"inbound": [RESTRICTED_PATH, OPEN_PATH]}
    out = egress.filter_withheld_entries(vault, payload, principal=_external())
    assert out["inbound"] == [OPEN_PATH]


def test_filter_withheld_entries_empty_policy_is_identity(vault: Path) -> None:
    payload = {"entries": [{"path": RESTRICTED_PATH}, {"path": OPEN_PATH}]}
    assert egress.filter_withheld_entries(
        vault, {"entries": list(payload["entries"])}, principal=_external()
    ) == payload


def test_filter_withheld_entries_blocked_policy_drops_everything(vault: Path) -> None:
    write_broken_policy(vault)
    payload = {"entries": [{"path": RESTRICTED_PATH}, {"path": OPEN_PATH}]}
    out = egress.filter_withheld_entries(vault, payload, principal=_external())
    assert out["entries"] == []


def test_filter_withheld_entries_unresolved_principal_drops_everything(
    vault: Path,
) -> None:
    from exomem.governance.principal import most_restrictive_principal

    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    payload = {"entries": [{"path": RESTRICTED_PATH}]}
    out = egress.filter_withheld_entries(
        vault, payload, principal=most_restrictive_principal(surface="rest")
    )
    assert out["entries"] == []


def _through_dispatcher(vault: Path, name: str, **kwargs):
    """Invoke a command the way every real surface does.

    MCP, REST, hosted and CLI all reach a leaf through
    `writer_lease.invoke_command`, which is where the structure/review
    surfaces are gated — so a test that calls the leaf directly would be
    testing a path no client uses.
    """
    from exomem.writer_lease import invoke_command

    command = next(c for c in commands.COMMANDS if c.name == name)
    return invoke_command(command, vault, **kwargs)


def test_browse_memory_does_not_leak_a_withheld_path(vault: Path) -> None:
    """`browse_memory` is not in `commands.COMMANDS` in this build, so it is
    driven through the exact composition the dispatcher applies: leaf, then
    `postfilter`."""
    _restricted_vault(vault)
    with request_scope(_external()):
        raw = commands.op_browse_memory(
            vault, path="Knowledge Base/Notes/Patterns", mode="list"
        )
        out = egress.postfilter("browse_memory", raw, vault)
    assert "kill-switch-for-risky-releases" not in str(out)


def test_list_directory_does_not_leak_a_withheld_path(vault: Path) -> None:
    _restricted_vault(vault)
    with request_scope(_external()):
        out = _through_dispatcher(
            vault, "list_directory", path="Knowledge Base/Notes/Patterns"
        )
    assert "kill-switch-for-risky-releases" not in str(out)


def test_list_inbound_links_does_not_leak_a_withheld_path(vault: Path) -> None:
    _restricted_vault(vault)
    with request_scope(_external()):
        out = _through_dispatcher(
            vault,
            "list_inbound_links",
            target="Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout.md",
        )
    assert "kill-switch-for-risky-releases" not in str(out)


def test_overview_does_not_leak_a_withheld_path(vault: Path) -> None:
    _restricted_vault(vault)
    with request_scope(_external()):
        out = _through_dispatcher(vault, "overview")
    assert "kill-switch-for-risky-releases" not in str(out)


def test_dispatcher_backstop_drops_withheld_entries(vault: Path) -> None:
    """H6: the postfilter contributed only the scrubber — the withheld
    cross-check returned early whenever a policy was active, so D1's
    dispatcher backstop did not exist. This is the check that should have
    caught the ungated `fetch` and the eight structure surfaces."""
    _restricted_vault(vault)
    payload = {"entries": [{"path": RESTRICTED_PATH}, {"path": OPEN_PATH}]}
    with request_scope(_external()):
        out = egress.postfilter("list_directory", payload, vault)
    assert [e["path"] for e in out["entries"]] == [OPEN_PATH]


def test_structure_surfaces_are_registered_content_returning() -> None:
    registry = {c.name: c for c in commands.COMMANDS}
    derived = egress.content_returning_commands(registry)
    for name in ("list_directory", "overview", "list_inbound_links", "audit", "attention"):
        assert name in derived, name
        assert egress._COMMAND_PROJECTOR_KIND[name] == "structure", name


# --- H8: token bound to the item's own ceiling, minted only when returned ---


def test_notice_token_is_bound_to_the_items_own_ceiling(vault: Path) -> None:
    """An item capped at L1 must not yield an L5-capable capability."""
    from exomem.governance import tokens

    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NOTICE)
    result = egress.annotate_hits(
        vault, [_hit(RESTRICTED_PATH)], principal=_external(), limit=1
    )
    assert len(result.notices) == 1
    claim = tokens.verify(
        vault, result.notices[0]["escalation_token"], audience=EXTERNAL
    )
    assert claim.max_level == egress.LEVEL_NOTICE
    assert claim.max_level < egress.RELEASE_FLOOR


def test_no_token_is_minted_for_a_truncated_notice(vault: Path) -> None:
    """Minting happened before truncation, so every withheld candidate wrote a
    token row — including the ones dropped a moment later."""
    import sqlite3

    from exomem.governance.store import sidecar_path

    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NOTICE)
    pool = [
        _hit(RESTRICTED_PATH),
        _hit("Knowledge Base/Notes/Patterns/retry-with-full-jitter-backoff.md"),
        _hit("Knowledge Base/Notes/Patterns/retry-with-fixed-interval.md"),
    ]
    result = egress.annotate_hits(vault, pool, principal=_external(), limit=1)
    assert len(result.notices) == 1
    with sqlite3.connect(sidecar_path(vault)) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM withhold_tokens").fetchone()[0]
    assert rows == 1, f"{rows} token rows written for 1 returned notice"


# --- H10: pack governance block is not an activity oracle ------------------


def test_pack_emits_no_governance_block_when_nothing_is_withheld(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    with request_scope(_external()):
        result = commands.op_find(vault, query="retry backoff jitter", limit=5, pack=True)
    assert "governance" not in result["pack"]


def test_pack_never_shows_the_policy_fingerprint_to_a_non_owner(vault: Path) -> None:
    """The fingerprint is a SHA-256 over the policy bytes — polling it reveals
    exactly when the owner retuned their rules."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NOTICE)
    with request_scope(_external()):
        result = commands.op_find(
            vault, query="kill switch risky releases", limit=1, pack=True
        )
    governance = result["pack"].get("governance")
    if governance is not None:
        assert "fingerprint" not in governance


def test_pack_shows_the_fingerprint_to_the_owner(vault: Path) -> None:
    """The `limit` is deliberately generous so a notice is actually RETURNED.

    N4: the governance block now keys on notices rather than on the withheld
    set. With `limit=1` the D4 backfill fills the slot and suppresses the
    notice — and a block emitted anyway would announce "something was
    withheld" precisely when backfill was hiding that fact, which is the
    oracle N4 closed. A notice-bearing request is the case where a fingerprint
    belongs at all.
    """
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NOTICE, audience="owner")
    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        result = commands.op_find(
            vault, query="kill switch risky releases", limit=10, pack=True
        )
    governance = result["pack"].get("governance")
    assert governance is not None, "a returned notice must carry the block"
    assert governance["notices"]
    assert governance["fingerprint"] not in ("missing", "")


def test_pack_block_is_suppressed_when_backfill_hid_the_withholding(
    vault: Path,
) -> None:
    """The N4 oracle in its subtler form: at `limit=1` the withheld slot is
    backfilled, no notice is returned, and the block must not appear — it
    would re-reveal exactly what the backfill was designed to conceal."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NOTICE, audience="owner")
    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        result = commands.op_find(
            vault, query="kill switch risky releases", limit=1, pack=True
        )
    pack = result.get("pack") or {}
    if not (pack.get("governance") or {}).get("notices"):
        assert "governance" not in pack


def test_sub_floor_notice_carries_no_path(vault: Path) -> None:
    """L17: `annotate_page` echoed the canonicalized path back on an L1-L3
    notice, confirming the exact location of an item the caller may only have
    guessed at through a fuzzy identifier."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NOTICE)
    notice = egress.annotate_page(
        vault, {"path": RESTRICTED_PATH, "body": "x", "frontmatter": {}},
        principal=_external(),
    )
    assert notice is not None
    assert notice["withheld"] is True
    assert "path" not in notice
    assert RESTRICTED_PATH not in str(notice)


# --------------------------------------------------------------------------
# filter_withheld_entries: dict VALUES, not just list entries
# --------------------------------------------------------------------------


def _governed_shut(vault: Path) -> None:
    """Withhold `Notes/Patterns/**` from `external` entirely."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)


def _filter_as_external(vault: Path, payload):
    return egress.filter_withheld_entries(
        vault,
        payload,
        principal=RequestPrincipal(audience_id=EXTERNAL, surface="mcp"),
    )


def test_withheld_path_in_a_dict_value_is_dropped(vault: Path) -> None:
    """The gap: the cross-check filtered LIST entries, so a withheld path
    reached the wire whenever it lived in a dict value instead.

    `adoption_run`'s `outcomes` is exactly this shape — a map keyed by source
    path whose values carry `target_path` — and `target_path` was not even in
    the path-field set, so neither half of the check could see it."""
    _governed_shut(vault)
    payload = {
        "outcomes": {
            "legacy/notes.md": {
                "status": "applied",
                "target_path": RESTRICTED_PATH,
            }
        }
    }
    out = _filter_as_external(vault, payload)
    assert RESTRICTED_PATH not in json.dumps(out, default=str)


def test_withheld_path_as_a_dict_KEY_is_dropped(vault: Path) -> None:
    """A map keyed BY vault path leaks through its keys, not its values."""
    _governed_shut(vault)
    payload = {"outcomes": {RESTRICTED_PATH: {"status": "applied"}}}
    out = _filter_as_external(vault, payload)
    assert RESTRICTED_PATH not in json.dumps(out, default=str)


@pytest.mark.parametrize(
    "field",
    [
        "target_path",
        "source_path",
        "original_path",
        "old_path",
        "new_path",
        "destination",
        "trash_path",
        "result_path",
        "predecessor_path",
        "resolved_target_path",
        "logical_target_path",
        "logical_source_path",
    ],
)
def test_every_enumerated_path_field_is_filtered_in_a_list_entry(
    vault: Path, field: str
) -> None:
    """The path-field set drove only `path`/`rel_path`/`file`/`target`/
    `parent_path`/`id`, so a mutation result reporting where a page moved to
    named a withheld page in the clear."""
    _governed_shut(vault)
    payload = {"entries": [{"status": "ok", field: RESTRICTED_PATH}]}
    out = _filter_as_external(vault, payload)
    assert RESTRICTED_PATH not in json.dumps(out, default=str), field


def test_a_nested_dict_naming_a_withheld_path_is_dropped(vault: Path) -> None:
    _governed_shut(vault)
    payload = {"result": {"path": RESTRICTED_PATH, "body": "secret prose"}}
    out = _filter_as_external(vault, payload)
    assert RESTRICTED_PATH not in json.dumps(out, default=str)


def test_permitted_dict_values_survive_the_filter(vault: Path) -> None:
    """Fail-closed must not mean fail-empty: a permitted neighbour in the same
    shapes is untouched."""
    _governed_shut(vault)
    payload = {
        "outcomes": {OPEN_PATH: {"status": "applied", "target_path": OPEN_PATH}},
        "entries": [{"status": "ok", "new_path": OPEN_PATH}],
    }
    out = _filter_as_external(vault, payload)
    assert out["outcomes"][OPEN_PATH]["target_path"] == OPEN_PATH
    assert out["entries"][0]["new_path"] == OPEN_PATH


def test_a_path_outside_the_vault_is_not_the_release_plane_s_business(
    vault: Path,
) -> None:
    """N6 corrects what I previously asserted here.

    I had pinned "cannot be resolved -> withheld" as a fail-closed reading.
    It is not fail-closed, it is fail-wrong: the release plane has no
    jurisdiction over something that is not a vault item, so deleting the
    entry that mentions it removes permitted content while protecting
    nothing. An adoption run's pre-import source paths are the everyday case.
    """
    _governed_shut(vault)
    payload = {"outcomes": {"legacy/never-imported.md": {"status": "applied"}}}
    out = _filter_as_external(vault, payload)
    assert out["outcomes"] == {"legacy/never-imported.md": {"status": "applied"}}


def test_a_resolvable_withheld_path_is_still_dropped_in_dict_shapes(
    vault: Path,
) -> None:
    """…and the jurisdiction that DOES exist is unchanged."""
    _governed_shut(vault)
    payload = {"outcomes": {RESTRICTED_PATH: {"status": "applied"}}}
    out = _filter_as_external(vault, payload)
    assert out["outcomes"] == {}


def test_the_top_level_payload_itself_is_never_dropped(vault: Path) -> None:
    """The filter operates on pairs WITHIN a mapping, never on the mapping it
    was handed — otherwise a single withheld field would erase an entire
    response envelope, including fields the caller is entitled to."""
    _governed_shut(vault)
    payload = {"path": RESTRICTED_PATH, "ok": True}
    out = _filter_as_external(vault, payload)
    assert isinstance(out, dict)
    assert out.get("ok") is True


def test_ungoverned_vault_dict_values_are_untouched(vault: Path) -> None:
    """The empty-policy fast path stays a no-op on every shape."""
    payload = {"outcomes": {"a.md": {"target_path": RESTRICTED_PATH}}}
    assert egress.filter_withheld_entries(vault, payload) == payload


# --------------------------------------------------------------------------
# N4 — the presence of the pack `governance` block is itself an L0 oracle
# --------------------------------------------------------------------------


def test_pack_governance_block_is_absent_when_there_are_no_notices() -> None:
    """At L0 the item is dropped SILENTLY and no notice is emitted (D4), so a
    `governance` block carrying an empty notices list says exactly one thing:
    "something was hidden from you". That is the existence oracle the silent
    L0 path exists to prevent — the block must key on notices, not on the
    withheld set."""
    release = egress.AnnotatedHits(
        hits=[],
        notices=[],
        withheld_paths=frozenset({RESTRICTED_PATH}),
        active=True,
    )
    pack = egress.annotate_pack({"packed_paths": []}, release)
    assert pack is not None
    assert "governance" not in pack


def test_pack_governance_block_is_present_when_a_notice_exists() -> None:
    """…and still appears whenever there is something to say."""
    release = egress.AnnotatedHits(
        hits=[],
        notices=[{"rule_id": "r", "scope": "Patterns"}],
        withheld_paths=frozenset({RESTRICTED_PATH}),
        active=True,
    )
    pack = egress.annotate_pack({"packed_paths": []}, release)
    assert pack is not None
    assert pack["governance"]["notices"]


# --------------------------------------------------------------------------
# N5 — the walker skipped tuples, sets and frozensets entirely
# --------------------------------------------------------------------------


def test_tuple_valued_fields_are_filtered(vault: Path) -> None:
    """`adopt` alone returns 18 tuple-valued fields, and a tuple was returned
    by identity — so every one of them was an unfiltered channel."""
    _governed_shut(vault)
    out = _filter_as_external(vault, {"rows": ({"path": RESTRICTED_PATH},)})
    assert RESTRICTED_PATH not in json.dumps(out, default=str)


def test_list_nested_inside_a_tuple_is_filtered(vault: Path) -> None:
    _governed_shut(vault)
    out = _filter_as_external(vault, {"rows": ([{"path": RESTRICTED_PATH}],)})
    assert RESTRICTED_PATH not in json.dumps(out, default=str)


def test_set_and_frozenset_values_are_filtered(vault: Path) -> None:
    _governed_shut(vault)
    for shape in (set, frozenset):
        out = _filter_as_external(vault, {"rows": shape({RESTRICTED_PATH})})
        assert RESTRICTED_PATH not in json.dumps(out, default=str), shape.__name__


def test_tuple_shape_is_preserved_for_permitted_content(vault: Path) -> None:
    """Filtering must not silently retype the payload."""
    _governed_shut(vault)
    out = _filter_as_external(vault, {"rows": ({"path": OPEN_PATH},)})
    assert isinstance(out["rows"], tuple)


# --------------------------------------------------------------------------
# N6 — "does not resolve under the vault" is NOT "withheld"
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/docs/readme.md",
        "exomem://source/Knowledge%20Base/Notes/Insights/whatever",
        "/etc/somewhere/external.md",
        "../outside-the-vault/notes.md",
    ],
)
def test_non_vault_md_shaped_values_do_not_delete_an_entry(
    vault: Path, value: str
) -> None:
    """`.md`-shaped strings that resolve to nothing under the vault were being
    treated as UNDECIDABLE and therefore withheld, so an external URL or a
    citation deleted an otherwise-permitted entry outright."""
    _governed_shut(vault)
    # `target_path` IS a tracked path field, so this genuinely exercises the
    # decision path rather than passing because nobody looked at the field.
    payload = {"entries": [{"path": OPEN_PATH, "target_path": value, "title": "keep me"}]}
    out = _filter_as_external(vault, payload)
    assert out["entries"], f"{value!r} deleted a permitted entry"
    assert out["entries"][0]["title"] == "keep me"


def test_one_unresolvable_field_does_not_delete_the_whole_entry(vault: Path) -> None:
    """`all(_permitted(...))` meant a single non-vault field vetoed the entry."""
    _governed_shut(vault)
    payload = {"entries": [{"path": OPEN_PATH, "target_path": "https://x.dev/a.md"}]}
    out = _filter_as_external(vault, payload)
    assert len(out["entries"]) == 1


def test_a_real_withheld_path_is_still_dropped_after_the_skip_rule(
    vault: Path,
) -> None:
    """The N6 relaxation must not become a bypass: a path that DOES resolve
    under the vault and is withheld still deletes the entry."""
    _governed_shut(vault)
    payload = {"entries": [{"path": OPEN_PATH, "target_path": RESTRICTED_PATH}]}
    out = _filter_as_external(vault, payload)
    assert out["entries"] == []


# --------------------------------------------------------------------------
# N7 — `_path_like` normalization (the 481278a class)
# --------------------------------------------------------------------------


def test_path_like_normalizes_separators_and_extension_case() -> None:
    # The backslash separator is built at runtime rather than written as a
    # literal: a backslash-separated path in source reads as a Windows
    # absolute path to `public_artifact_privacy`'s local-path rule, which then
    # reports the test file itself as leaking a local path.
    sep = chr(92)
    assert egress._path_like(f"Knowledge Base{sep}Notes{sep}Patterns{sep}x.md") == (
        "Knowledge Base/Notes/Patterns/x.md"
    )
    assert egress._path_like("Knowledge Base/Notes/Patterns/x.MD") is not None
    assert egress._path_like("Knowledge Base/Notes/Patterns/x.Md") is not None
    assert egress._path_like("not a path at all") is None
    assert egress._path_like(None) is None


def test_windows_separator_variant_is_filtered(vault: Path) -> None:
    """Decisive on any platform: after normalization this names the same real
    file, so it must be decided rather than sailing past the shape test."""
    _governed_shut(vault)
    payload = {"entries": [{"path": RESTRICTED_PATH.replace("/", "\\")}]}
    out = _filter_as_external(vault, payload)
    assert out["entries"] == []


# --------------------------------------------------------------------------
# N1 — overview/browse: counts and samples derive from a WALK, so the
# dispatcher's entry filter structurally cannot gate them
# --------------------------------------------------------------------------

PATTERNS_DIR = "Knowledge Base/Notes/Patterns"


def _browse_overview(vault: Path, *, path: str, principal):
    from exomem.governance.principal import request_scope

    with request_scope(principal):
        return commands.op_browse_memory(vault, path=path, mode="overview")


def test_browse_overview_of_a_withheld_folder_is_not_the_owner_s_report(
    vault: Path,
) -> None:
    """N1: the whole finding in one assertion.

    `browse_memory(mode="overview", path=<withheld folder>)` returned a
    response byte-identical to the owner's — totals, tree node, `largest[]`,
    `oldest_unmodified[]` and `sample_names` all intact for a `ceiling: 0`
    folder. Entry-level filtering at the dispatcher cannot fix this, because
    the numbers are computed by a walk that never knew about the policy.
    """
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    owner = RequestPrincipal(audience_id="owner", surface="cli")
    _reset_caches()
    owner_report = _browse_overview(vault, path=PATTERNS_DIR, principal=owner)
    _reset_caches()
    try:
        external_report = _browse_overview(vault, path=PATTERNS_DIR, principal=_external())
    except ValueError as exc:
        assert "NOT_FOUND" in str(exc)
        return
    assert external_report != owner_report, (
        "a restricted audience received the owner's byte-identical report "
        "for a fully withheld folder"
    )


def test_browse_overview_counts_do_not_leak_a_withheld_folder(vault: Path) -> None:
    """The architecturally important half: `files_direct: 1` beside
    `sample_names: []` is a STRONGER oracle than the sample list, because it
    states the exact number of things being hidden."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    try:
        report = _browse_overview(vault, path="", principal=_external())
    except ValueError as exc:
        assert "NOT_FOUND" in str(exc)
        return
    blob = json.dumps(report, default=str)
    assert "kill-switch-for-risky-releases" not in blob
    nodes = report.get("tree") or []
    for node in nodes:
        if str(node.get("path", "")).endswith("Notes/Patterns"):
            assert node.get("files_direct", 0) == 0, (
                f"withheld folder still reports its file count: {node}"
            )


def test_browse_overview_largest_and_oldest_omit_withheld_files(vault: Path) -> None:
    """`largest[]` and `oldest_unmodified[]` carry BARE filenames in a `path`
    field, which the dispatcher's bare-name rule never consulted."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    try:
        report = _browse_overview(vault, path=PATTERNS_DIR, principal=_external())
    except ValueError as exc:
        assert "NOT_FOUND" in str(exc)
        return
    for section in ("largest", "oldest_unmodified"):
        for row in report.get(section) or []:
            assert "kill-switch" not in str(row.get("path", "")), (
                f"{section} leaked a withheld file: {row}"
            )


def test_browse_overview_still_serves_the_owner(vault: Path) -> None:
    """The gate is a release decision, not a blanket denial."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    report = _browse_overview(
        vault, path=PATTERNS_DIR, principal=RequestPrincipal(audience_id="owner", surface="cli")
    )
    assert "kill-switch-for-risky-releases" in json.dumps(report, default=str)


def test_ungoverned_overview_is_untouched(vault: Path) -> None:
    """Zero behaviour change and zero policy work on an ungoverned vault."""
    _reset_caches()
    baseline = commands.op_browse_memory(vault, path="", mode="overview")
    assert baseline["totals"]["files"] > 0


# --------------------------------------------------------------------------
# N2 — list_inbound_links echoes the withheld TARGET back
# --------------------------------------------------------------------------


def _inbound(vault: Path, target: str, principal):
    from exomem.governance.principal import request_scope

    with request_scope(principal):
        return commands.op_list_inbound_links(vault, target=target)


def _link_to_restricted(vault: Path) -> None:
    """A permitted note that wikilinks the restricted one."""
    source = vault / "Knowledge Base" / "Notes" / "Insights" / "links-out.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "---\ntype: insight\n---\n"
        "See [[kill-switch-for-risky-releases]] for the rollback stance.\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "target",
    [
        RESTRICTED_PATH,
        RESTRICTED_PATH[: -len(".md")],
        "kill-switch-for-risky-releases",
    ],
)
def test_inbound_links_to_a_withheld_target_reads_as_no_links(
    vault: Path, target: str
) -> None:
    """N2: the entry survived because its own `path` is the PERMITTED source
    note, so the dispatcher filter had no reason to drop it — while
    `raw_target` carried the withheld stem and `context` carried the full
    withheld path. `count: 1` vs `0` settles it on its own.

    Absence shape: this surface deliberately does not require the target to
    exist ("what links to this file I'm about to delete?"), so a missing
    target already returns an EMPTY result rather than an error. Matching that
    makes withheld indistinguishable from both no-links and never-existed;
    raising NOT_FOUND here would invent a signal the surface never emits.
    """
    _link_to_restricted(vault)
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    result = _inbound(vault, target, _external())
    assert result["count"] == 0, f"leaked inbound count for {target!r}"
    assert result["inbound"] == []
    # The response may echo what the CALLER sent and nothing more. In
    # particular a bare basename must not come back resolved to its canonical
    # vault path, which would confirm the location of a withheld page.
    blob = json.dumps(result, default=str)
    assert "links-out" not in blob, f"leaked the linking source via {target!r}"
    assert "raw_target" not in blob and "context" not in blob
    if "/" not in target:
        assert "Knowledge Base" not in blob, (
            f"bare name {target!r} came back resolved to a canonical path"
        )


def test_inbound_links_withheld_matches_the_no_links_shape(vault: Path) -> None:
    """Byte-identical to a target that genuinely has no inbound links."""
    _link_to_restricted(vault)
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    withheld = _inbound(vault, RESTRICTED_PATH, _external())
    _reset_caches()
    never_existed = _inbound(vault, "Knowledge Base/Notes/Patterns/nope.md", _external())
    assert withheld["count"] == never_existed["count"] == 0
    assert withheld["inbound"] == never_existed["inbound"] == []


def test_inbound_links_still_answer_the_owner(vault: Path) -> None:
    _link_to_restricted(vault)
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    result = _inbound(
        vault, RESTRICTED_PATH, RequestPrincipal(audience_id="owner", surface="cli")
    )
    assert result["count"] >= 1


def test_inbound_links_ungoverned_is_untouched(vault: Path) -> None:
    _link_to_restricted(vault)
    _reset_caches()
    result = commands.op_list_inbound_links(vault, target=RESTRICTED_PATH)
    assert result["count"] >= 1


def test_adopt_scan_inherits_the_walk_gate(vault: Path) -> None:
    """`adopt`'s scan_summary counts come from `overview()`, so gating the
    walk covers it without a second bespoke edit."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    from exomem.governance.principal import request_scope

    with request_scope(_external()):
        report = commands.op_adopt(vault, path="", mode="scan-only")
    assert "kill-switch-for-risky-releases" not in json.dumps(report, default=str)


def test_bare_filename_in_a_path_field_is_resolved_against_its_node(
    vault: Path,
) -> None:
    """N1(a) at the dispatcher: `largest[]`/`oldest_unmodified[]` carry a BARE
    filename in a `path` field, which `_bare_name` was never consulted for —
    it only ever looked at list elements that were themselves bare strings."""
    _governed_shut(vault)
    payload = {
        "path": "Knowledge Base/Notes/Patterns",
        "largest": [{"path": "kill-switch-for-risky-releases.md", "bytes": 955}],
    }
    out = _filter_as_external(vault, payload)
    assert out["largest"] == []


def test_subtree_root_node_with_empty_path_still_filters_bare_names(
    vault: Path,
) -> None:
    """N1(b): a subtree-root node carries `path: ""`, and `_directory_of`
    returned `None` for it — silently disabling the bare-name filter at the
    one node the caller actually asked about."""
    _governed_shut(vault)
    payload = {"path": "", "sample_names": ["kill-switch-for-risky-releases.md"]}
    egress.filter_withheld_entries(
        vault,
        payload,
        principal=RequestPrincipal(audience_id=EXTERNAL, surface="mcp"),
    )
    # At the vault root the bare name resolves to a root-level file, which
    # does not exist — so it is not a vault item and survives. The load-bearing
    # assertion is that `""` is treated as a real directory rather than as
    # "no directory known", which is what `_directory_of` now returns.
    assert egress._directory_of({"path": ""}) == ""
    assert egress._directory_of({"path": "Notes/Patterns"}) == "Notes/Patterns"
    assert egress._directory_of({"nope": 1}) is None


# --------------------------------------------------------------------------
# NEW-1 / NEW-2 — `_decide_path` returns None for any non-markdown, and None
# is overloaded to mean BOTH "unreadable" and "not permitted"
# --------------------------------------------------------------------------

# A real 1x1 PNG. A synthetic UTF-8-decodable ".mp4" does NOT reproduce these
# findings: `find_corpus.parse_page` succeeds on decodable bytes, so the whole
# failure mode disappears. The 0x89 lead byte is an invalid UTF-8 start.
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)


def _media(vault: Path, rel: str = "Knowledge Base/Notes/Patterns/deck.png") -> str:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_1X1)
    return rel


def test_withheld_media_is_pruned_from_the_walk(vault: Path) -> None:
    """NEW-1: `keep()` returned True for every non-`.md`, so a restricted
    audience saw withheld media enumerated in `largest`, `oldest_unmodified`,
    `sample_names`, `files_direct`, `binary` and `totals`."""
    _media(vault)
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    keep = egress.release_walk_filter(vault, principal=_external())
    assert keep is not None
    assert keep("Knowledge Base/Notes/Patterns/deck.png") is False


def test_a_folder_of_withheld_media_still_collapses(vault: Path) -> None:
    """The compounding half: withheld media kept the folder non-empty, so the
    scoped-probe refusal only ever fired for a markdown-ONLY folder. A folder
    holding a deck, a recording and a cap table returned a full valid report."""
    _media(vault, "Knowledge Base/Notes/Patterns/deck.png")
    _media(vault, "Knowledge Base/Notes/Patterns/recording.mp4")
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    with pytest.raises(ValueError, match="NOT_FOUND"):
        _browse_overview(vault, path=PATTERNS_DIR, principal=_external())


def test_overview_binary_counts_do_not_leak_withheld_media(vault: Path) -> None:
    _media(vault)
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    report = _browse_overview(vault, path="", principal=_external())
    assert "deck.png" not in json.dumps(report, default=str)


def test_permitted_media_still_downloads_for_the_owner(vault: Path) -> None:
    """NEW-2, the functionality break: `release_allows_download` read the same
    `None` as deny, so the moment a user created `_Governance/`, media
    downloads broke for EVERYONE — the owner included."""
    rel = _media(vault, "Knowledge Base/Notes/Insights/chart.png")
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    from exomem.governance.principal import request_scope

    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        assert egress.release_allows_download(vault, rel) is True
        assert egress.release_allows_frames(vault, rel) is True


def test_withheld_media_download_is_still_denied(vault: Path) -> None:
    """…and the gate must still deny the media that IS in a withheld scope."""
    rel = _media(vault, "Knowledge Base/Notes/Patterns/deck.png")
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    from exomem.governance.principal import request_scope

    with request_scope(_external()):
        assert egress.release_allows_download(vault, rel) is False
        assert egress.release_allows_frames(vault, rel) is False


def test_media_decision_emits_no_utf8_decode_noise(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Deciding a binary must not go anywhere near the markdown parser."""
    import logging

    rel = _media(vault)
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    with caplog.at_level(logging.WARNING):
        egress.release_level_for(vault, rel, principal=_external())
    assert "utf-8" not in caplog.text.lower()


# --------------------------------------------------------------------------
# NEW-3 — `overview` enumerated the policy files themselves
# --------------------------------------------------------------------------


def test_governance_dir_never_surfaces_in_overview(vault: Path) -> None:
    """The analogue of `test_governance_dir_never_surfaces_in_find`.

    `_Governance/` holds YAML, and YAML is not `.md`, so the walk enumerated
    the policy filenames themselves — `rules/['r.yaml']`,
    `scopes/['patterns.yaml']` — to a restricted audience. Policy files are
    never vault content for ANY audience, so they leave the walk outright
    rather than being decided.
    """
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    report = _browse_overview(vault, path="", principal=_external())
    blob = json.dumps(report, default=str)
    assert "_Governance" not in blob
    assert "patterns.yaml" not in blob
    assert "patterns-external.yaml" not in blob


def test_governance_dir_is_hidden_from_the_owner_too(vault: Path) -> None:
    """Not an audience decision: the policy tree is infrastructure, and the
    owner reads it through the governance surfaces, not through a file walk."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL, audience="owner")
    _reset_caches()
    report = _browse_overview(
        vault, path="", principal=RequestPrincipal(audience_id="owner", surface="cli")
    )
    assert "_Governance" not in json.dumps(report, default=str)


def test_governance_dir_hidden_on_an_ungoverned_walk(vault: Path) -> None:
    """Also true with no policy at all — a stray `_Governance/` directory is
    still not content, and the exclusion must not depend on the release gate
    being active."""
    gov = vault / "Knowledge Base" / "_Governance" / "scopes"
    gov.mkdir(parents=True, exist_ok=True)
    (gov / "stray.yaml").write_text("governance_version: 1\n", encoding="utf-8")
    _reset_caches()
    report = commands.op_browse_memory(vault, path="", mode="overview")
    assert "_Governance" not in json.dumps(report, default=str)


# --------------------------------------------------------------------------
# NEW-4 — the N6 skip-not-deny contract reopened N7's variant class
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("uppercase_extension", RESTRICTED_PATH[: -len(".md")] + ".MD"),
        ("mixed_extension", RESTRICTED_PATH[: -len(".md")] + ".Md"),
        ("percent_encoded_space", RESTRICTED_PATH.replace(" ", "%20")),
        ("percent_encoded_upper", RESTRICTED_PATH.replace(" ", "%20")[: -len(".md")] + ".MD"),
        ("backslash", RESTRICTED_PATH.replace("/", chr(92))),
        ("leading_dot_slash", "./" + RESTRICTED_PATH),
        ("double_slash", RESTRICTED_PATH.replace("/Notes/", "//Notes//")),
        ("uppercase_whole_path", RESTRICTED_PATH.upper()),
    ],
)
def test_every_path_variant_resolves_to_the_same_withheld_item(
    vault: Path, label: str, value: str
) -> None:
    """NEW-4: `_normalize_pathish` matches the SHAPE case-insensitively, so
    `.MD` and percent-encoded forms became candidates — but `_is_vault_item`
    then resolved them with a raw `is_file()`, which is case-sensitive on
    Linux and does no percent-decoding. Under the N6 skip-not-deny contract an
    unresolvable variant now SURVIVES, which is platform-dependent behaviour:
    the same payload leaks on Linux and is filtered on macOS. Same class as
    shipped fix 481278a.
    """
    _governed_shut(vault)
    out = _filter_as_external(vault, {"entries": [{"path": value}]})
    assert out["entries"] == [], f"{label} variant survived the filter"


def test_genuinely_external_md_references_still_survive(vault: Path) -> None:
    """The N6 contract must survive the NEW-4 tightening: a reference that
    names nothing in this vault is still not the release plane's business."""
    _governed_shut(vault)
    for value in (
        "https://example.com/docs/readme.md",
        "/etc/elsewhere/external.md",
        "../outside-the-vault/notes.md",
        "Knowledge Base/Notes/Patterns/does-not-exist-at-all.md",
    ):
        out = _filter_as_external(
            vault, {"entries": [{"path": OPEN_PATH, "target_path": value}]}
        )
        assert len(out["entries"]) == 1, f"{value!r} deleted a permitted entry"


def test_permitted_variants_are_not_over_blocked(vault: Path) -> None:
    """Variant resolution must not turn into a blanket drop: the same variant
    forms of a PERMITTED page still resolve and still pass."""
    _governed_shut(vault)
    for value in (OPEN_PATH, OPEN_PATH[: -len(".md")] + ".MD", OPEN_PATH.replace(" ", "%20")):
        out = _filter_as_external(vault, {"entries": [{"path": value}]})
        assert len(out["entries"]) == 1, f"{value!r} was wrongly dropped"


# --------------------------------------------------------------------------
# BLOCKER 1 — `_Governance` is still enumerable by two sibling paths
# --------------------------------------------------------------------------

GOV_DIR = "Knowledge Base/_Governance"


def _browse_list(vault: Path, *, path: str, principal, recursive: bool = True):
    from exomem.governance.principal import request_scope

    with request_scope(principal):
        return commands.op_browse_memory(
            vault, path=path, mode="list", recursive=recursive
        )


@pytest.mark.parametrize("audience", ["owner", "external"])
def test_scoped_overview_probe_into_governance_is_refused(
    vault: Path, audience: str
) -> None:
    """BLOCKER 1(a): `_SKIP_ALWAYS` prunes `_Governance` only as a CHILD
    dirname during the walk, so pointing the scan root AT it walks it — the
    directory is never a child of itself. Returned `['r.yaml','s.yaml']`,
    `totals {files: 2, dirs: 2, bytes: 222}` and the per-dir tree."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    who = (
        RequestPrincipal(audience_id="owner", surface="cli")
        if audience == "owner"
        else _external()
    )
    with pytest.raises(ValueError, match="NOT_FOUND"):
        _browse_overview(vault, path=GOV_DIR, principal=who)


def test_scoped_overview_probe_into_a_governance_subdir_is_refused(
    vault: Path,
) -> None:
    write_scope(vault)
    _reset_caches()
    with pytest.raises(ValueError, match="NOT_FOUND"):
        _browse_overview(vault, path=f"{GOV_DIR}/scopes", principal=_external())


def test_scoped_overview_probe_into_governance_is_refused_ungoverned(
    vault: Path,
) -> None:
    """Audience-independent AND policy-independent: a stray `_Governance/`
    on an ungoverned vault is still not content."""
    gov = vault / "Knowledge Base" / "_Governance" / "scopes"
    gov.mkdir(parents=True, exist_ok=True)
    (gov / "stray.yaml").write_text("governance_version: 1\n", encoding="utf-8")
    _reset_caches()
    with pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_browse_memory(vault, path=GOV_DIR, mode="overview")


@pytest.mark.parametrize("audience", ["owner", "external"])
def test_list_mode_never_shows_the_governance_dir(vault: Path, audience: str) -> None:
    """BLOCKER 1(b): `mode="list"` had no exclusion at all."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    who = (
        RequestPrincipal(audience_id="owner", surface="cli")
        if audience == "owner"
        else _external()
    )
    result = _browse_list(vault, path="Knowledge Base", principal=who)
    assert "_Governance" not in json.dumps(result, default=str)


def test_list_mode_hides_governance_on_an_ungoverned_vault(vault: Path) -> None:
    gov = vault / "Knowledge Base" / "_Governance" / "rules"
    gov.mkdir(parents=True, exist_ok=True)
    (gov / "stray.yaml").write_text("governance_version: 1\n", encoding="utf-8")
    _reset_caches()
    result = commands.op_browse_memory(
        vault, path="Knowledge Base", mode="list", recursive=True
    )
    assert "_Governance" not in json.dumps(result, default=str)


def test_list_mode_scan_root_inside_governance_is_refused(vault: Path) -> None:
    write_scope(vault)
    _reset_caches()
    with pytest.raises(ValueError, match="NOT_FOUND"):
        _browse_list(vault, path=GOV_DIR, principal=_external())


# --------------------------------------------------------------------------
# BLOCKER 2 — `..` regression, and percent-decoding ordered too late
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("dotdot_inside", "Knowledge Base/Notes/Insights/../Patterns/kill-switch-for-risky-releases.md"),
        ("dotdot_twice", "Knowledge Base/Notes/Insights/sub/../../Patterns/kill-switch-for-risky-releases.md"),
        ("percent_encoded_slash", "Knowledge%20Base%2FNotes%2FPatterns%2Fkill-switch-for-risky-releases.md"),
        ("double_encoded", "Knowledge%2520Base/Notes/Patterns/kill-switch-for-risky-releases.md"),
        ("trailing_dot", "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md."),
    ],
)
def test_traversal_and_encoding_variants_still_resolve(
    vault: Path, label: str, value: str
) -> None:
    """BLOCKER 2: rejecting any `..` outright means `None`, and under the
    skip-not-deny contract `None` means KEEP — so a relative link that lands
    squarely inside the vault now survives, where `.resolve()` used to drop
    it. `[x](../Patterns/foo.md)` is the standard relative markdown link, so
    this is ordinary authoring, not an attack shape.

    `%2F` never reached the resolver at all: `_path_like`'s raw-slash test ran
    BEFORE any decoding, so an encoded separator failed the shape test."""
    _governed_shut(vault)
    out = _filter_as_external(vault, {"entries": [{"path": value}]})
    assert out["entries"] == [], f"{label} variant survived the filter"


@pytest.mark.parametrize(
    "value",
    [
        "../../../etc/passwd.md",
        "Knowledge Base/../../outside.md",
        "../outside-the-vault/notes.md",
    ],
)
def test_traversal_that_escapes_the_root_is_still_rejected(
    vault: Path, value: str
) -> None:
    """The fold must not become a bypass: a `..` chain that leaves the vault
    resolves to nothing here and stays out of the release plane's business."""
    _governed_shut(vault)
    out = _filter_as_external(
        vault, {"entries": [{"path": OPEN_PATH, "target_path": value}]}
    )
    assert len(out["entries"]) == 1, f"{value!r} deleted a permitted entry"


# --------------------------------------------------------------------------
# DEFECT B — `links.outbound` names withheld pages by bare stem
# --------------------------------------------------------------------------


def test_outbound_links_do_not_name_a_withheld_page_by_stem(vault: Path) -> None:
    """A wikilink field stores BARE stems, and the bare-word asymmetry that is
    right for prose is wrong here: inside a reference list every entry is
    definitionally a reference, so the stem must be compared."""
    source = vault / "Knowledge Base" / "Notes" / "Insights" / "links-out.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "---\ntype: insight\n---\nSee [[kill-switch-for-risky-releases]].\n",
        encoding="utf-8",
    )
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    from exomem.governance.principal import request_scope

    with request_scope(_external()):
        page = commands.op_read_memory(
            vault, path="Knowledge Base/Notes/Insights/links-out.md", links=True
        )
    # Scoped to the STRUCTURED links field. The rendered body still quotes the
    # wikilink, and body-text scanning of a released page is explicitly out of
    # scope for this change — a released page's prose is its own content.
    assert "kill-switch-for-risky-releases" not in json.dumps(
        page.get("links"), default=str
    )


def test_outbound_links_keep_permitted_stems(vault: Path) -> None:
    """Still a release decision: a permitted neighbour stays linked."""
    source = vault / "Knowledge Base" / "Notes" / "Insights" / "links-ok.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "---\ntype: insight\n---\nSee [[rrf-fusion-beats-score-normalization]].\n",
        encoding="utf-8",
    )
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    from exomem.governance.principal import request_scope

    with request_scope(_external()):
        page = commands.op_read_memory(
            vault, path="Knowledge Base/Notes/Insights/links-ok.md", links=True
        )
    assert "rrf-fusion-beats-score-normalization" in json.dumps(page, default=str)


def test_a_bare_stem_shared_by_two_pages_fails_closed(vault: Path) -> None:
    """Ambiguity resolves to WITHHELD, deliberately.

    `Notes/.../shared-name.md` is permitted and `Patterns/shared-name.md` is
    withheld. A bare wikilink `[[shared-name]]` cannot tell us which page the
    author meant — and inside a reference list the bare-word asymmetry that
    protects prose does not apply. Failing closed costs one over-blocked
    reference entry; failing open leaks the existence of a withheld page.
    """
    permitted_twin = vault / "Knowledge Base" / "Notes" / "Insights" / "shared-name.md"
    permitted_twin.parent.mkdir(parents=True, exist_ok=True)
    permitted_twin.write_text("---\ntype: insight\n---\ntwin\n", encoding="utf-8")
    withheld_twin = vault / "Knowledge Base" / "Notes" / "Patterns" / "shared-name.md"
    withheld_twin.parent.mkdir(parents=True, exist_ok=True)
    withheld_twin.write_text("---\ntype: pattern\n---\ntwin\n", encoding="utf-8")

    source = vault / "Knowledge Base" / "Notes" / "Insights" / "cites-twin.md"
    source.write_text(
        "---\ntype: insight\n---\nSee [[shared-name]].\n", encoding="utf-8"
    )
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    from exomem.governance.principal import request_scope

    with request_scope(_external()):
        page = commands.op_read_memory(
            vault, path="Knowledge Base/Notes/Insights/cites-twin.md", links=True
        )
    assert "shared-name" not in json.dumps(page.get("links"), default=str), (
        "an ambiguous bare stem matching a withheld page must fail closed"
    )


# --------------------------------------------------------------------------
# BLOCKER — the guard landed on the ENUMERATION surfaces and missed the READS
# --------------------------------------------------------------------------

GOV_RULE = "Knowledge Base/_Governance/rules/patterns-external.yaml"
GOV_SCOPE = "Knowledge Base/_Governance/scopes/patterns.yaml"


def _plant_gov_markdown(vault: Path) -> str:
    rel = "Knowledge Base/_Governance/notes.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\ntype: source\n---\ngovernance-body-marker\n", encoding="utf-8")
    return rel


@pytest.mark.parametrize("audience", ["owner", "external"])
@pytest.mark.parametrize("target", [GOV_RULE, GOV_SCOPE])
def test_get_refuses_to_read_the_policy_tree(
    vault: Path, audience: str, target: str
) -> None:
    """The constrained party could read the exact rules constraining them.

    `annotate_page` cannot catch this: the policy tree is in no scope, so it
    decides at L6 and releases the file in full. `rules/` and `scopes/` are
    fixed scaffold conventions, so filename guessing is a low bar.
    """
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    who = (
        RequestPrincipal(audience_id="owner", surface="cli")
        if audience == "owner"
        else _external()
    )
    from exomem.governance.principal import request_scope

    with request_scope(who), pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_get(vault, path=target)


@pytest.mark.parametrize("audience", ["owner", "external"])
def test_fetch_refuses_to_read_the_policy_tree(vault: Path, audience: str) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()
    who = (
        RequestPrincipal(audience_id="owner", surface="cli")
        if audience == "owner"
        else _external()
    )
    from exomem.governance.principal import request_scope

    with request_scope(who), pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_fetch(vault, id=GOV_RULE)


def test_get_refuses_markdown_planted_inside_the_policy_tree(vault: Path) -> None:
    """A `.md` inside `_Governance/` is still policy-tree territory."""
    rel = _plant_gov_markdown(vault)
    write_scope(vault)
    _reset_caches()
    from exomem.governance.principal import request_scope

    with request_scope(_external()), pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_get(vault, path=rel)


@pytest.mark.parametrize("leaf", ["get", "fetch"])
def test_policy_tree_read_refusal_matches_the_ungoverned_vault(
    vault: Path, leaf: str
) -> None:
    """Ungoverned parity — this is what keeps the refusal from BEING the
    oracle. If a governed vault refused and an ungoverned one served, the
    refusal itself would announce that governance is active."""
    gov = vault / "Knowledge Base" / "_Governance" / "rules"
    gov.mkdir(parents=True, exist_ok=True)
    (gov / "stray.yaml").write_text("governance_version: 1\n", encoding="utf-8")
    _reset_caches()
    call = (
        (lambda: commands.op_get(vault, path="Knowledge Base/_Governance/rules/stray.yaml"))
        if leaf == "get"
        else (lambda: commands.op_fetch(vault, id="Knowledge Base/_Governance/rules/stray.yaml"))
    )
    with pytest.raises(ValueError, match="NOT_FOUND"):
        call()


def test_ordinary_reads_are_unaffected(vault: Path) -> None:
    """The guard must not touch anything outside the policy tree."""
    _reset_caches()
    page = commands.op_get(vault, path=OPEN_PATH)
    assert page["path"] == OPEN_PATH


# --------------------------------------------------------------------------
# Non-blocking 1 — a symlink into the policy tree bypasses the overview guard
# --------------------------------------------------------------------------


def test_overview_refuses_a_symlink_into_the_policy_tree(vault: Path) -> None:
    """`rel` carries no `_Governance` component, so the name-based guard never
    fires — and `os.walk` follows the SCAN ROOT even with `followlinks=False`.
    The containment check has to be on the resolved root."""
    write_scope(vault)
    link = vault / "Knowledge Base" / "Notes" / "gov-link"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(vault / "Knowledge Base" / "_Governance")
    except OSError:
        pytest.skip("symlinks unavailable")
    _reset_caches()
    with pytest.raises(ValueError, match="NOT_FOUND"):
        _browse_overview(vault, path="Knowledge Base/Notes/gov-link", principal=_external())


# --------------------------------------------------------------------------
# Non-blocking 2 — a file literally named with a percent escape
# --------------------------------------------------------------------------


def test_a_percent_literal_filename_resolves_to_itself(vault: Path) -> None:
    """`_decode_pathish` ran before the exact-hit check, so `a%20b.md` could
    never resolve to itself — a reference to the withheld percent-literal file
    resolved to its permitted decoded twin and was kept."""
    patterns = vault / "Knowledge Base" / "Notes" / "Patterns"
    patterns.mkdir(parents=True, exist_ok=True)
    (patterns / "a%20b.md").write_text("---\ntype: pattern\n---\nliteral\n", encoding="utf-8")
    insights = vault / "Knowledge Base" / "Notes" / "Insights"
    insights.mkdir(parents=True, exist_ok=True)
    (insights / "a b.md").write_text("---\ntype: insight\n---\ntwin\n", encoding="utf-8")
    _governed_shut(vault)
    out = _filter_as_external(
        vault, {"entries": [{"path": "Knowledge Base/Notes/Patterns/a%20b.md"}]}
    )
    assert out["entries"] == [], "the percent-literal file resolved to its decoded twin"
