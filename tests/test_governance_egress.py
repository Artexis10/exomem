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

from exomem import commands, referent_resolution, referent_runtime
from exomem import find as find_module
from exomem.find_types import GraphProvenance, Hit
from exomem.governance import bridges, egress, receipts
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

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


def write_scope(
    vault: Path,
    *,
    paths: str = PATTERNS_GLOB,
    name: str = "Patterns",
    default_deny: bool = False,
) -> None:
    target = _gov_dir(vault) / "scopes" / "patterns.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: {name}\npaths: [\"{paths}\"]\n"
        + ("default_deny: true\n" if default_deny else ""),
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


def _receipt_records(vault: Path) -> list[dict[str, object]]:
    events = _gov_dir(vault) / "events"
    if not events.exists():
        return []
    return [
        json.loads(line)
        for path in sorted(events.rglob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


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


def _read_alias_surface(vault: Path, *, path: str, surface: str) -> object:
    from exomem import get_frontmatter as get_frontmatter_module
    from exomem import get_page as get_page_module

    if surface == "get":
        return commands.op_get(vault, path=path)
    if surface == "fetch":
        return commands.op_fetch(vault, id=path)
    if surface == "frontmatter":
        return commands.op_get(vault, path=path, frontmatter_only=True)
    if surface == "get-page":
        return get_page_module.get_page(vault, path=path)
    if surface == "get-frontmatter":
        return get_frontmatter_module.get_frontmatter(vault, path=path)
    raise AssertionError(f"unexpected read surface: {surface}")


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


def test_find_hot_cache_stays_principal_free(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two audiences, second call served from the hot cache: decisions are
    recomputed per request and NO cached candidate copy carries a prior
    audience's decision."""
    write_scope(vault)
    write_rule(vault, ceiling=0)
    query = "kill switch risky releases for two people"
    resolver_calls = 0
    original_resolver = referent_runtime.resolve_for_find

    def counted_resolver(*args, **kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return original_resolver(*args, **kwargs)

    monkeypatch.setattr(referent_runtime, "resolve_for_find", counted_resolver)

    with request_scope(_external()):
        commands.op_find(vault, query=query, limit=10)
    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        second = commands.op_find(vault, query=query, limit=10)

    # The owner sees the restricted page the external audience could not.
    second_hits = second["hits"] if isinstance(second, dict) else second
    assert RESTRICTED_PATH in [h["path"] for h in second_hits]

    # Every Hit sitting in the shared hot cache is principal-free.
    cached_hits = [
        hit for cached in find_module._FIND_CACHE.values() for hit in cached
    ]
    assert cached_hits, "expected the hot cache to be populated"
    assert all(getattr(hit, "decision", None) is None for hit in cached_hits)
    assert all("referents" not in hit.as_dict() for hit in cached_hits)
    assert resolver_calls == 2, "referents must be recomputed on the hot-cache hit"


def test_referents_never_name_withheld_entity_pages(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    release = egress.AnnotatedHits(
        hits=[_hit(OPEN_PATH)],
        withheld_paths=frozenset({RESTRICTED_PATH}),
        active=True,
    )
    block = {
        "status": "resolved",
        "entity_type": "person",
        "resolved": [
            {"path": RESTRICTED_PATH, "title": "Hidden", "entity_type": "person", "evidence": []}
        ],
        "candidates": [
            {"path": OPEN_PATH, "title": "Open", "entity_type": "person", "evidence": []}
        ],
        "reasons": {},
    }
    with request_scope(_external()):
        guarded = egress.guard_referents(vault, block, release)
    assert guarded is not None
    assert [item["path"] for item in guarded["candidates"]] == [OPEN_PATH]
    assert RESTRICTED_PATH not in str(guarded)


def test_referents_drop_evidence_naming_withheld_anchor_seeds(vault: Path) -> None:
    release = egress.AnnotatedHits(
        hits=[_hit(OPEN_PATH)],
        withheld_paths=frozenset({RESTRICTED_PATH}),
        active=True,
    )
    block = {
        "status": "resolved",
        "entity_type": "person",
        "resolved": [
            {
                "path": OPEN_PATH,
                "title": "Open",
                "entity_type": "person",
                "evidence": [
                    {"kind": "graph", "seed": RESTRICTED_PATH, "relation_type": "relates_to"},
                    {"kind": "attribute", "matched": ["friend"]},
                ],
            }
        ],
        "candidates": [],
        "reasons": {},
    }
    guarded = egress.guard_referents(vault, block, release)
    assert guarded is not None
    evidence = guarded["resolved"][0]["evidence"]
    assert evidence == [{"kind": "attribute", "matched": ["friend"]}]
    assert RESTRICTED_PATH not in str(guarded)


def test_referents_block_omitted_for_blocked_audience(vault: Path) -> None:
    write_broken_policy(vault)
    release = egress.AnnotatedHits(
        hits=[],
        withheld_paths=frozenset({RESTRICTED_PATH, OPEN_PATH}),
        active=True,
        blocked=True,
    )
    block = {
        "status": "resolved",
        "entity_type": "person",
        "resolved": [{"path": OPEN_PATH, "title": "Open", "evidence": []}],
        "candidates": [],
        "reasons": {},
    }
    with request_scope(_external()):
        assert egress.guard_referents(vault, block, release) is None


def test_referents_drop_tombstoned_entities_and_evidence_even_when_policy_is_empty(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tombstoned_entity = "Knowledge Base/Entities/People/tombstoned.md"
    tombstoned_anchor = "Knowledge Base/Notes/Research/tombstoned-anchor.md"
    monkeypatch.setattr(
        egress.lifecycle,
        "is_tombstoned",
        lambda _vault, path: path in {tombstoned_entity, tombstoned_anchor},
    )
    release = egress.AnnotatedHits(hits=[], active=False)
    block = {
        "status": "resolved",
        "entity_type": "person",
        "resolved": [
            {
                "path": tombstoned_entity,
                "title": "Tombstoned",
                "entity_type": "person",
                "evidence": [{"kind": "exact_name", "matched": "Tombstoned"}],
            }
        ],
        "candidates": [
            {
                "path": OPEN_PATH,
                "title": "Open",
                "entity_type": "person",
                "evidence": [
                    {"kind": "graph", "seed": tombstoned_anchor},
                    {"kind": "attribute", "matched": ["friend"]},
                ],
            }
        ],
        "reasons": {},
    }

    guarded = egress.guard_referents(vault, block, release)

    assert guarded is not None
    assert guarded["resolved"] == []
    assert guarded["candidates"][0]["evidence"] == [
        {"kind": "attribute", "matched": ["friend"]}
    ]
    assert tombstoned_entity not in str(guarded)
    assert tombstoned_anchor not in str(guarded)


def test_referents_release_decisions_are_receipted(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    release = egress.AnnotatedHits(hits=[], active=True)
    block = {
        "status": "resolved",
        "entity_type": "person",
        "resolved": [
            {"path": RESTRICTED_PATH, "title": "Hidden", "evidence": []}
        ],
        "candidates": [{"path": OPEN_PATH, "title": "Open", "evidence": []}],
        "reasons": {},
    }

    with request_scope(_external()), egress.disclosure_boundary(vault, "find") as collector:
        egress.guard_referents(vault, block, release)
        egress.emit_boundary_receipt(collector)

    outcomes = _receipt_records(vault)[0]["outcomes"]
    assert {item["decision"] for item in outcomes} == {"released", "withheld"}


def test_referents_block_omitted_when_guard_withholds_every_match(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.referent_resolution import EntityRecord

    entity_path = "Knowledge Base/Entities/People/hidden-entity.md"
    write_scope(vault, paths="Entities/People/**", name="People")
    write_rule(vault, ceiling=0)
    entity = EntityRecord(
        path=entity_path,
        title="Hidden Entity",
        entity_type="person",
        status="active",
    )
    monkeypatch.setattr(
        referent_runtime,
        "load_entity_registry",
        lambda *_args, **_kwargs: {entity_path: entity},
    )
    release = egress.AnnotatedHits(hits=[], active=True)

    with request_scope(_external()):
        block = referent_runtime.resolve_for_find(
            vault,
            query="who was Hidden Entity",
            hits=[],
            mode="hybrid",
            graph=False,
            release=release,
            purpose=None,
        )

    assert block is None


def test_referents_honour_release_strip_decisions(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=6)
    identity = bridges.StripIdentity(
        path=OPEN_PATH,
        ref="exomem://memory/open",
        title="Open",
    )
    decision = egress.Decision(
        level=egress.LEVEL_FULL,
        release_strip=(identity,),
    )
    monkeypatch.setattr(egress, "_decide_path", lambda *_args, **_kwargs: decision)
    block = {
        "status": "resolved",
        "entity_type": "person",
        "resolved": [
            {
                "path": OPEN_PATH,
                "title": "Open",
                "entity_type": "person",
                "ref": "exomem://memory/open",
                "evidence": [{"kind": "exact_name", "matched": "Open"}],
            }
        ],
        "candidates": [],
        "reasons": {},
    }

    with request_scope(owner_principal()):
        guarded = egress.guard_referents(
            vault,
            block,
            egress.AnnotatedHits(hits=[], active=True),
        )

    assert guarded is not None
    assert guarded["resolved"][0]["path"] == OPEN_PATH
    assert guarded["resolved"][0]["title"] == "Open"
    assert "ref" not in guarded["resolved"][0]
    assert guarded["resolved"][0]["evidence"] == []


def test_referents_block_is_byte_identical_for_withheld_and_absent_entities(
    tmp_path: Path,
) -> None:
    withheld_vault = tmp_path / "withheld"
    absent_vault = tmp_path / "absent"
    for candidate_vault in (withheld_vault, absent_vault):
        write_scope(candidate_vault, paths="Entities/People/**", name="People")
        write_rule(candidate_vault, ceiling=0)

    candidates = [
        {
            "path": f"Knowledge Base/Entities/People/synthetic-{index:02d}.md",
            "title": f"Synthetic {index:02d}",
            "entity_type": "person",
            "evidence": [{"kind": "attribute", "matched": ["friend"]}],
        }
        for index in range(referent_resolution.REFERENT_CANDIDATE_CAP)
    ]
    withheld_block = {
        "status": "unresolved",
        "entity_type": "person",
        "resolved": [],
        "candidates": candidates,
        "reasons": {"inactive": 1, "type_mismatch": 0},
        "expected_count": 2,
        "unresolved_count": 2,
        "omitted_candidate_count": 1,
    }
    absent_block = {
        "status": "unresolved",
        "entity_type": "person",
        "resolved": [],
        "candidates": [],
        "reasons": {"inactive": 0, "type_mismatch": 0},
        "expected_count": 2,
        "unresolved_count": 2,
    }

    with request_scope(_external()):
        guarded_withheld = egress.guard_referents(
            withheld_vault,
            withheld_block,
            egress.AnnotatedHits(hits=[], active=True),
        )
        guarded_absent = egress.guard_referents(
            absent_vault,
            absent_block,
            egress.AnnotatedHits(hits=[], active=True),
        )

    assert guarded_withheld is not None
    assert guarded_absent is not None
    assert json.dumps(guarded_withheld, sort_keys=True) == json.dumps(
        guarded_absent, sort_keys=True
    )


def test_referents_block_drops_counters_when_only_tombstones_activate_the_gate(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        egress.lifecycle,
        "tombstoned_paths",
        lambda _vault: {"Knowledge Base/Entities/People/deleted.md"},
    )
    block = {
        "status": "unresolved",
        "entity_type": "person",
        "resolved": [],
        "candidates": [],
        "reasons": {"inactive": 1, "type_mismatch": 0},
        "expected_count": 2,
        "unresolved_count": 2,
        "omitted_candidate_count": 4,
    }

    guarded = egress.guard_referents(
        vault,
        block,
        egress.AnnotatedHits(hits=[], active=True),
    )

    assert guarded is not None
    assert "reasons" not in guarded
    assert "omitted_candidate_count" not in guarded


def test_referents_counters_present_on_ungoverned_vault_without_tombstones(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate_calls = 0
    original_gate_state = egress.gate_state

    def counted_gate_state(vault_root: Path):
        nonlocal gate_calls
        gate_calls += 1
        return original_gate_state(vault_root)

    monkeypatch.setattr(egress, "gate_state", counted_gate_state)
    block = {
        "status": "unresolved",
        "entity_type": "person",
        "resolved": [],
        "candidates": [],
        "reasons": {"inactive": 2, "type_mismatch": 0},
        "expected_count": 2,
        "unresolved_count": 2,
        "omitted_candidate_count": 3,
    }

    guarded = egress.guard_referents(
        vault,
        block,
        egress.AnnotatedHits(hits=[], active=False),
    )

    assert guarded is not None
    assert guarded["reasons"] == {"inactive": 2, "type_mismatch": 0}
    assert guarded["omitted_candidate_count"] == 3
    assert gate_calls == 1


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


@pytest.mark.parametrize(
    ("initially_restricted", "initial_marker", "replacement_marker"),
    [
        (True, "restricted-snapshot-marker", "public-live-marker"),
        (False, "public-snapshot-marker", "restricted-live-marker"),
    ],
)
def test_get_fails_closed_when_page_swaps_before_release_annotation(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    initially_restricted: bool,
    initial_marker: str,
    replacement_marker: str,
) -> None:
    """The decision, hash, ref, receipt, and returned body are one snapshot.

    A deterministic swap in either direction between the read leaf and the
    release plane must not authorize one representation and return another.
    """
    rel = "Knowledge Base/Notes/Insights/snapshot-race.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)

    def _page(*, restricted: bool, marker: str, page_id: str) -> str:
        tags = "tags: [restricted]\n" if restricted else "tags: [public]\n"
        return (
            "---\ntype: insight\n"
            f"exomem_id: {page_id}\n{tags}---\n\n{marker}\n"
        )

    initial = _page(
        restricted=initially_restricted,
        marker=initial_marker,
        page_id="00000000-0000-4000-8000-000000000101",
    )
    replacement = _page(
        restricted=not initially_restricted,
        marker=replacement_marker,
        page_id="00000000-0000-4000-8000-000000000102",
    )
    target.write_text(initial, encoding="utf-8")
    scope = _gov_dir(vault) / "scopes" / "patterns.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Restricted\n"
        "tags: [restricted]\n",
        encoding="utf-8",
    )
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _reset_caches()

    original = egress.annotate_page

    def _swap_before_annotation(*args, **kwargs):
        target.write_text(replacement, encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(egress, "annotate_page", _swap_before_annotation)
    with request_scope(_external()), pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_get(vault, path=rel)


def test_direct_read_receipt_is_bound_to_returned_snapshot(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/receipt-snapshot.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: insight\n"
        "exomem_id: 00000000-0000-4000-8000-000000000103\n"
        "---\n\nsnapshot receipt body\n",
        encoding="utf-8",
    )
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    _reset_caches()
    with request_scope(_external()), egress.disclosure_boundary(vault, "get") as collector:
        page = commands.op_get(vault, path=rel)
        egress.emit_boundary_receipt(collector)
    outcome = _receipt_records(vault)[0]["outcomes"][0]
    assert outcome["content_hash"] == page["content_hash"]
    assert outcome["size"] == len(target.read_bytes())
    assert outcome.get("ref") == page.get("ref")


def test_direct_read_never_attaches_a_replacement_page_ref(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/Notes/Insights/ref-race.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    initial = "---\ntype: insight\n---\n\noriginal snapshot\n"
    replacement = (
        "---\ntype: insight\n"
        "exomem_id: 00000000-0000-4000-8000-000000000104\n"
        "---\n\nreplacement snapshot\n"
    )
    target.write_text(initial, encoding="utf-8")
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    _reset_caches()
    original_annotate = egress.annotate_page

    def _swap_after_annotation(*args, **kwargs):
        released = original_annotate(*args, **kwargs)
        target.write_text(replacement, encoding="utf-8")
        return released

    monkeypatch.setattr(egress, "annotate_page", _swap_after_annotation)
    with request_scope(_external()):
        page = commands.op_get(vault, path=rel)
    assert page["body"] == "original snapshot\n"
    assert "ref" not in page


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
# Reverse provenance — `ingested_into` (release-gate task 4)
#
# Provenance runs both ways. `sources` names what a compiled note cited;
# `ingested_into` names every compiled note that cited a source (note.py
# appends the new note's wikilink to each cited source on every compile). A
# source released to an audience that cannot see those notes therefore
# enumerates them — the release channel running in reverse.
# --------------------------------------------------------------------------

SOURCE_SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FC0"
SOURCE_RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FC1"
INGESTED_SOURCE_PATH = (
    "Knowledge Base/Sources/Articles/2026-06-02-postgres-autovacuum-tuning.md"
)
RESTRICTED_TITLE = "Kill switch for risky releases"


def _write_sources_scope_and_rule(vault: Path, *, ceiling: int) -> None:
    """A SECOND scope/rule pair, so the cited source and the compiled note that
    cites it can sit at different ceilings — the shape the leak needs."""
    scopes = _gov_dir(vault) / "scopes"
    scopes.mkdir(parents=True, exist_ok=True)
    (scopes / "sources.yaml").write_text(
        f"governance_version: 1\nid: {SOURCE_SCOPE_ID}\nname: Sources\n"
        'paths: ["Sources/**"]\n',
        encoding="utf-8",
    )
    rules = _gov_dir(vault) / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "sources-external.yaml").write_text(
        f"governance_version: 1\nid: {SOURCE_RULE_ID}\n"
        f'scope_ids: ["{SOURCE_SCOPE_ID}"]\n'
        f"audience: {EXTERNAL}\nceiling: {ceiling}\n",
        encoding="utf-8",
    )


def _plant_reverse_citation(vault: Path, entry: str) -> None:
    """Write the exact back-ref `note.py` appends to every cited source."""
    target = vault / INGESTED_SOURCE_PATH
    original = target.read_text(encoding="utf-8")
    updated = original.replace("ingested_into: []", f'ingested_into:\n  - "{entry}"')
    assert updated != original, "fixture no longer carries an empty ingested_into"
    target.write_text(updated, encoding="utf-8")


#: Every form the back-ref can be written in. `note.py` writes the wikilink
#: forms; the `.md` path form is what a hand-edited or migrated vault carries.
REVERSE_CITATION_FORMS = (
    f"[[{RESTRICTED_PATH.removesuffix('.md')}]]",
    f"[[{RESTRICTED_KB_RELATIVE.removesuffix('.md')}]]",
    f"[[{RESTRICTED_STEM}]]",
    RESTRICTED_PATH,
)


@pytest.mark.parametrize("entry", REVERSE_CITATION_FORMS)
def test_released_source_does_not_enumerate_the_notes_that_ingested_it(
    vault: Path, entry: str
) -> None:
    """A source released BELOW full to an audience that cannot see the note
    compiled from it must not name that note in any reference form."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _write_sources_scope_and_rule(vault, ceiling=egress.LEVEL_EXCERPT)
    _plant_reverse_citation(vault, entry)
    _reset_caches()

    with request_scope(_external()):
        out = commands.op_get(vault, path=INGESTED_SOURCE_PATH)

    assert out is not None, "the source itself is released; only its back-ref is not"
    blob = json.dumps(out, default=str)
    assert "ingested_into" not in json.dumps(
        out.get("frontmatter") or {}, default=str
    ), f"reverse citation survived release for form {entry!r}"
    for form in (
        RESTRICTED_PATH,
        RESTRICTED_KB_RELATIVE,
        RESTRICTED_STEM,
        RESTRICTED_TITLE,
    ):
        assert form not in blob, f"{form!r} leaked through ingested_into ({entry!r})"


def test_released_source_keeps_a_reverse_citation_to_a_permitted_note(
    vault: Path,
) -> None:
    """The strip is a release decision, not a blanket deletion: when the note
    that ingested the source is itself permitted, the field survives intact."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    _write_sources_scope_and_rule(vault, ceiling=egress.LEVEL_FULL)
    _plant_reverse_citation(vault, f"[[{RESTRICTED_PATH.removesuffix('.md')}]]")
    _reset_caches()

    with request_scope(_external()):
        out = commands.op_get(vault, path=INGESTED_SOURCE_PATH)

    assert out is not None
    ingested = (out.get("frontmatter") or {}).get("ingested_into")
    assert ingested == [f"[[{RESTRICTED_PATH.removesuffix('.md')}]]"]


def test_reverse_citation_is_in_the_frontmatter_provenance_strip_set() -> None:
    """Structural: the field set is what `_strip_page_provenance` iterates, so
    a reverse citation that is not in it is never even considered."""
    assert "ingested_into" in egress._FRONTMATTER_PROVENANCE_FIELDS


def test_reverse_citation_leak_is_closed_on_the_real_dispatch_path(
    vault: Path,
) -> None:
    """The production path. MCP, REST, hosted and CLI all reach `op_get`
    through `writer_lease.invoke_command`, and the wikilink form the vault
    actually writes cleared every gate on that path before this change — the
    dispatcher backstop included."""
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    _write_sources_scope_and_rule(vault, ceiling=egress.LEVEL_EXCERPT)
    _plant_reverse_citation(vault, f"[[{RESTRICTED_PATH.removesuffix('.md')}]]")
    _reset_caches()

    with request_scope(_external()):
        out = _through_dispatcher(vault, "get", path=INGESTED_SOURCE_PATH)

    assert out is not None
    assert RESTRICTED_STEM not in json.dumps(out, default=str)


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


def test_structure_filter_gates_governed_csv_and_resource_entries(vault: Path) -> None:
    csv_path = "Knowledge Base/Notes/Patterns/private.csv"
    target = vault / csv_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("name,value\nAlice,42\n", encoding="utf-8")
    _restricted_vault(vault)
    with request_scope(_external()):
        out = egress.postfilter(
            "list_directory",
            {"entries": [{"path": csv_path, "size": target.stat().st_size}], "resource": csv_path},
            vault,
        )
    assert out["entries"] == []
    assert csv_path not in str(out)


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


def test_dispatcher_emits_one_plaintext_free_receipt_after_final_governed_representation(
    vault: Path,
) -> None:
    """The owning dispatcher, not the reusable postfilter, records egress."""
    _restricted_vault(vault)
    with request_scope(_external()):
        _through_dispatcher(vault, "list_directory", path="Knowledge Base/Notes/Patterns")

    records = _receipt_records(vault)
    assert len(records) == 1
    record = records[0]
    assert record["event_type"] == "disclosure"
    assert record["phase"] == "recorded"
    outcomes = record["outcomes"]
    assert isinstance(outcomes, list) and outcomes
    assert outcomes[0]["decision"] == "withheld"
    serialized = json.dumps(record)
    assert "kill-switch-for-risky-releases" not in serialized
    assert "Patterns" not in serialized


def test_postfilter_second_pass_is_receipt_silent(vault: Path) -> None:
    """MCP runs this filter twice, so it cannot own receipt emission."""
    _restricted_vault(vault)
    payload = {"entries": [{"path": RESTRICTED_PATH}]}
    with request_scope(_external()):
        egress.postfilter("list_directory", payload, vault)
        egress.postfilter("list_directory", payload, vault)
    assert _receipt_records(vault) == []


def test_ungoverned_dispatcher_recall_writes_no_receipt(vault: Path) -> None:
    with request_scope(_external()):
        _through_dispatcher(vault, "list_directory", path="Knowledge Base/Notes/Patterns")
    assert _receipt_records(vault) == []


def test_external_dispatcher_retries_mint_distinct_boundary_ids(vault: Path) -> None:
    _restricted_vault(vault)
    with request_scope(_external()):
        _through_dispatcher(vault, "list_directory", path="Knowledge Base/Notes/Patterns")
        _through_dispatcher(vault, "list_directory", path="Knowledge Base/Notes/Patterns")
    records = _receipt_records(vault)
    assert len(records) == 2
    assert records[0]["event_id"] != records[1]["event_id"]


def test_governed_dispatcher_fails_closed_when_receipt_append_fails(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _restricted_vault(vault)

    def fail(*_args, **_kwargs):
        raise receipts.ReceiptError("sidecar unavailable")

    monkeypatch.setattr(receipts, "append_event", fail)
    with request_scope(_external()):
        with pytest.raises(egress.ReceiptUnavailableError, match="retry"):
            _through_dispatcher(vault, "list_directory", path="Knowledge Base/Notes/Patterns")
    assert _receipt_records(vault) == []


def test_registry_refuses_new_content_command_without_receipt_adapter() -> None:
    registry = {"future_mode": object()}
    with pytest.raises(RuntimeError, match="RECEIPT_OUTCOME_MISSING"):
        egress.assert_outcomes_registered(registry)


def test_every_content_command_declares_a_receipt_outcome_adapter() -> None:
    registry = {command.name: command for command in commands.COMMANDS}
    assert egress.unrecorded_commands(registry) == ()


def test_credential_block_is_receipted_without_a_policy(vault: Path) -> None:
    secret = "Authorization: Bearer sk-proj-9dQm2XvKpLzR4wTnBcYeF8aHgJ1sVuNiO0rEyMdA"
    with request_scope(_external("credential-review")):
        with egress.disclosure_boundary(vault, "test") as collector:
            cleaned = egress.postfilter("test", {"value": secret}, vault)
            egress.emit_boundary_receipt(collector)
    records = _receipt_records(vault)
    assert cleaned["value"] != secret
    assert len(records) == 1 and records[0]["event_type"] == "credential_block"
    assert records[0]["command"] == "test"
    assert records[0]["principal"] == records[0]["audience"] == EXTERNAL
    assert records[0]["purpose"] == "credential-review"
    assert secret not in json.dumps(records)


def test_credential_block_does_not_suppress_the_disclosure_receipt(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    secret = "Authorization: Bearer sk-proj-9dQm2XvKpLzR4wTnBcYeF8aHgJ1sVuNiO0rEyMdA"
    with request_scope(_external("credential-review")):
        with egress.disclosure_boundary(vault, "find") as collector:
            egress.annotate_hits(
                vault, [_hit(RESTRICTED_PATH)], principal=_external("credential-review"), limit=1
            )
            egress.postfilter("find", {"value": secret}, vault)
            egress.emit_boundary_receipt(collector)
    records = _receipt_records(vault)
    assert [record["event_type"] for record in records] == [
        "credential_block", "disclosure"
    ]
    assert records[0]["command"] == "find"
    assert records[0]["principal"] == records[0]["audience"] == EXTERNAL
    assert records[0]["purpose"] == "credential-review"
    assert secret not in json.dumps(records)


def test_direct_download_boundary_emits_a_single_authorization_receipt(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    assert egress.release_allows_download(vault, RESTRICTED_PATH, principal=_external())
    records = _receipt_records(vault)
    assert len(records) == 1
    assert records[0]["event_type"] == "disclosure"
    assert records[0]["outcomes"][0]["decision"] == "release_authorized"


def test_nested_disclosure_boundaries_are_independent_unless_joined(vault: Path) -> None:
    other = vault.parent / "other-vault"

    with egress.disclosure_boundary(vault, "outer") as outer:
        egress._record_blocked_outcome(EXTERNAL)
        with egress.disclosure_boundary(vault, "inner") as inner:
            assert inner is not outer
            egress._record_blocked_outcome(EXTERNAL)
            egress.emit_boundary_receipt(inner)
        egress.emit_boundary_receipt(outer)
    with egress.disclosure_boundary(other, "cross-vault") as cross:
        egress._record_blocked_outcome(EXTERNAL)
        egress.emit_boundary_receipt(cross)

    assert [record["outcomes"][0]["command"] for record in _receipt_records(vault)] == [
        "inner",
        "outer",
    ]
    assert _receipt_records(other)[0]["outcomes"][0]["command"] == "cross-vault"

    with egress.disclosure_boundary(vault, "joined-outer") as outer:
        with egress.disclosure_boundary(vault, "joined-inner", join_existing=True) as joined:
            assert joined is outer
        with pytest.raises(RuntimeError, match="different vault"):
            with egress.disclosure_boundary(other, "bad-join", join_existing=True):
                pass


def test_hit_receipt_describes_only_the_final_limited_representation(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    hits = [_hit(RESTRICTED_PATH), _hit(OPEN_PATH)]
    with egress.disclosure_boundary(vault, "find") as collector:
        egress.annotate_hits(vault, hits, principal=_external(), limit=1)
        egress.emit_boundary_receipt(collector)
    outcomes = _receipt_records(vault)[0]["outcomes"]
    assert len(outcomes) == 1


def test_large_reduction_receipt_uses_truthful_bounded_aggregates(vault: Path) -> None:
    with egress.disclosure_boundary(vault, "overview") as collector:
        for _ in range(140):
            egress._record_outcome({"decision": "withheld"})
        egress.emit_boundary_receipt(collector)
    outcomes = _receipt_records(vault)[0]["outcomes"]
    assert outcomes[0]["decision"] == "withheld"
    assert outcomes[0]["count"] == 140
    assert len(outcomes[0]["membership_digest"]) == 64


def test_bounded_outcomes_keep_different_levels_and_scopes_distinct() -> None:
    outcomes = [
        egress.DisclosureOutcome({"decision": "released", "level": 5, "scope_ids": ["a"], "content_hash": "a" * 64})
        for _ in range(70)
    ] + [
        egress.DisclosureOutcome({"decision": "released", "level": 6, "scope_ids": ["b"], "content_hash": "b" * 64})
        for _ in range(70)
    ]
    reduced = egress._bounded_outcomes(outcomes)
    assert {(item["level"], tuple(item["scope_ids"]), item["count"]) for item in reduced} == {
        (5, ("a",), 70), (6, ("b",), 70)
    }
    assert all("membership_digest" in item for item in reduced)


def test_bounded_outcomes_summarize_129_distinct_typed_identities(vault: Path) -> None:
    outcomes = [
        egress.DisclosureOutcome(
            {
                "decision": "released",
                "level": 5 if index < 70 else 6,
                "principal": f"principal-{index}",
                "audience": f"audience-{index}",
                "purpose": f"purpose-{index}",
                "policy_fingerprint": f"{index:064x}",
                "confirmation": "none" if index % 2 else "confirmed",
                "scope_ids": [f"scope-{index}"],
                "command": f"boundary-{index}",
                "content_hash": f"{index + 1000:064x}",
            }
        )
        for index in range(129)
    ]
    reduced = egress._bounded_outcomes(outcomes)
    reversed_reduced = egress._bounded_outcomes(list(reversed(outcomes)))
    assert reduced == reversed_reduced
    assert len(reduced) <= receipts.MAX_OUTCOMES
    assert {(item["decision"], item["level"], item["count"]) for item in reduced} == {
        ("released", 5, 70),
        ("released", 6, 59),
    }
    for item in reduced:
        for field in (
            "identity_manifest_digest",
            "principal_set_digest",
            "audience_set_digest",
            "purpose_set_digest",
            "policy_set_digest",
            "confirmation_set_digest",
            "scope_set_digest",
            "boundary_set_digest",
        ):
            assert len(item[field]) == 64
    serialized = json.dumps(reduced)
    assert "principal-0" not in serialized
    assert "purpose-128" not in serialized
    with egress.disclosure_boundary(vault, "large-typed-boundary") as collector:
        collector.outcomes.extend(outcomes)
        egress.emit_boundary_receipt(collector)
    persisted = _receipt_records(vault)[0]["outcomes"]
    assert persisted == reduced
    assert len(persisted) <= receipts.MAX_OUTCOMES


@pytest.mark.parametrize("outcome_count", [64, 140])
def test_bounded_outcomes_also_fit_the_receipt_byte_cap(
    vault: Path, outcome_count: int
) -> None:
    scope_ids = [f"scope-{index}-" + "x" * 220 for index in range(128)]
    scope_digests = [f"{index:064x}" for index in range(128)]
    outcomes = [
        egress.DisclosureOutcome(
            {
                "decision": "released" if index % 2 else "withheld",
                "level": index % 7,
                "principal": "p" * 200,
                "audience": "a" * 200,
                "purpose": "u" * 200,
                "policy_fingerprint": "f" * 64,
                "confirmation": "none",
                "scope_ids": scope_ids,
                "scope_label_digests": scope_digests,
                "command": "large-boundary",
                "content_hash": f"{index + 5000:064x}",
            }
        )
        for index in range(outcome_count)
    ]
    with egress.disclosure_boundary(vault, "large-boundary") as collector:
        collector.outcomes.extend(outcomes)
        egress.emit_boundary_receipt(collector)
    events = _gov_dir(vault) / "events"
    raw = next(events.rglob("*.jsonl")).read_bytes()
    assert len(raw) <= receipts.MAX_RECORD_BYTES
    persisted = _receipt_records(vault)[0]["outcomes"]
    assert len(persisted) <= receipts.MAX_OUTCOMES
    assert {item["level"] for item in persisted} == set(range(7))


def test_disclosure_identity_is_content_free_and_complete(vault: Path) -> None:
    write_scope(vault, name="Sensitive scope")
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    with egress.disclosure_boundary(vault, "find") as collector:
        egress.annotate_hits(
            vault, [_hit(RESTRICTED_PATH)], principal=_external("analysis-purpose"), limit=1
        )
        egress.emit_boundary_receipt(collector)
    record = _receipt_records(vault)[0]
    outcome = record["outcomes"][0]
    assert len(record["event_id"]) == 32
    assert outcome["command"] == "find"
    assert outcome["principal"] == outcome["audience"] == EXTERNAL
    assert outcome["purpose"] == "analysis-purpose"
    assert outcome["confirmation"] == "none"
    assert outcome["scope_label_digests"]
    serialized = json.dumps(record)
    assert "Sensitive scope" not in serialized
    assert "kill-switch-for-risky-releases" not in serialized


def test_query_data_csv_is_a_registered_receipt_representation() -> None:
    assert egress.data_representation_adapter("rows") == "dataset"
    with pytest.raises(RuntimeError, match="RECEIPT_OUTCOME_MISSING"):
        egress.assert_data_representation_covered("future_rows")


def test_adoption_selector_requires_an_explicit_egress_adapter() -> None:
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "adoption_studio")
    with pytest.raises(RuntimeError, match="RECEIPT_OUTCOME_MISSING"):
        commands.invocation_is_read_only(command, {"action": "future-read"})


def test_every_mixed_selector_uses_one_complete_receipt_registry() -> None:
    expected = {
        ("connect_memory", "operation"): {
            "suggest-links": True,
            "suggest-relations": True,
            "context": True,
            "graph-context": True,
            "inbound-links": True,
            "resolve-entity": True,
            "create-entity": False,
            "accept-relation": False,
        },
        ("adopt_vault", "mode"): {
            "scan-only": True,
            "save-manifest": False,
            "copy-as-sources": False,
            "compile-selected": False,
        },
        ("adoption_studio", "action"): {
            "start": False,
            "status": True,
            "select": False,
            "plan": False,
            "apply": False,
            "cancel": False,
            "finish": False,
            "work-item": True,
            "propose": False,
            "apply-proposal": False,
        },
        ("process_media", "operation"): {
            "process": False,
            "status": True,
            "retry": False,
        },
        ("observe_memory", "operation"): {
            "add": False,
            "update": False,
            "remove": False,
            "validate": True,
        },
        ("maintain_memory", "mode"): {
            "audit": True,
            "fix": True,
            "reconcile": False,
            "backfill-ids": True,
        },
    }
    product = {command.name: command for command in commands.PRODUCT_COMMANDS}
    registry = egress.selector_registry()
    assert set(expected) <= set(registry)
    for (command_name, selector), values in expected.items():
        assert set(registry[(command_name, selector)]) == set(values)
        for value, read_only in values.items():
            adapter = egress.assert_selector_covered(command_name, selector, value)
            if adapter in {"structure", "mutation"}:
                assert (adapter != "mutation") is read_only
            assert commands.invocation_is_read_only(
                product[command_name], {selector: value}
            ) is read_only
        with pytest.raises(RuntimeError, match="RECEIPT_OUTCOME_MISSING"):
            commands.invocation_is_read_only(
                product[command_name], {selector: "future-selector"}
            )


def test_conditional_mixed_selectors_are_in_the_same_registry() -> None:
    product = {command.name: command for command in commands.PRODUCT_COMMANDS}
    registry = egress.selector_registry()
    assert registry[("manage_memory_file", "operation")] == {
        "list": "structure",
        "create": "validation",
        "append": "validation",
        "move": "mutation",
        "delete": "mutation",
        "trash-list": "structure",
        "recover": "mutation",
        "reclassify": "mutation",
        "propose-reclassification": "structure",
    }
    manage = product["manage_memory_file"]
    assert commands.invocation_is_read_only(manage, {"operation": "list"})
    assert commands.invocation_is_read_only(manage, {"operation": "trash-list"})
    assert commands.invocation_is_read_only(
        manage, {"operation": "propose-reclassification"}
    )
    assert not commands.invocation_is_read_only(manage, {"operation": "reclassify"})
    assert commands.invocation_is_read_only(
        manage, {"operation": "create", "validate_only": True}
    )
    assert not commands.invocation_is_read_only(
        manage, {"operation": "create", "validate_only": False}
    )

    assert registry[("schema_memory", "operation")] == {
        "infer": "save-conditional",
        "validate": "structure",
        "diff": "structure",
    }
    schema = product["schema_memory"]
    assert commands.invocation_is_read_only(schema, {"operation": "infer"})
    assert not commands.invocation_is_read_only(
        schema, {"operation": "infer", "save": True}
    )
    assert commands.invocation_is_read_only(schema, {"operation": "validate"})
    with pytest.raises(RuntimeError, match="RECEIPT_OUTCOME_MISSING"):
        commands.invocation_is_read_only(schema, {"operation": "future-schema-mode"})

    maintain = product["maintain_memory"]
    assert commands.invocation_is_read_only(maintain, {"mode": "fix"})
    assert not commands.invocation_is_read_only(
        maintain, {"mode": "fix", "dry_run": False}
    )
    assert not commands.invocation_is_read_only(maintain, {"mode": "reconcile"})
    assert commands.invocation_is_read_only(
        maintain, {"mode": "reconcile", "dry_run": True}
    )
    assert commands.invocation_is_read_only(maintain, {"mode": "backfill-ids"})


def test_query_data_csv_rows_are_gated_and_receipted(vault: Path) -> None:
    dataset = "Knowledge Base/Notes/Patterns/private.csv"
    target = vault / dataset
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("name,value\nAlice,42\n", encoding="utf-8")
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    with request_scope(_external()):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_query_data(vault, path=dataset)

    write_rule(vault, ceiling=egress.LEVEL_FULL)
    _reset_caches()
    with request_scope(_external()):
        with egress.disclosure_boundary(vault, "query_data") as collector:
            payload = commands.op_query_data(vault, path=dataset)
            egress.emit_boundary_receipt(collector)
    assert payload["rows"] == [{"name": "Alice", "value": "42"}]
    record = _receipt_records(vault)[0]
    assert record["outcomes"][0]["decision"] == "released"
    assert "Alice" not in json.dumps(record)


def test_receipt_conflict_never_turns_a_committed_mutation_into_a_retry_error(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_FULL)
    events = _gov_dir(vault) / "events"
    events.mkdir(parents=True, exist_ok=True)
    (events / "conflicted copy.jsonl").write_text("{}\n", encoding="utf-8")
    result = _through_dispatcher(
        vault,
        "create_file",
        path="Knowledge Base/Notes/receipt-mutation.md",
        content="committed body",
    )
    assert (vault / "Knowledge Base/Notes/receipt-mutation.md").exists()
    assert "GOVERNANCE_RECEIPT_UNAVAILABLE" not in str(result)


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


def test_a_withheld_page_is_filtered_however_the_reference_spells_its_case(
    vault: Path,
) -> None:
    """A reference is decided against the page it names, not its spelling.

    The companion guard to the over-blocking case above. `_governed_shut`
    withholds `Notes/Patterns/**` and leaves the rest open, so a resolver that
    hands back a miscased spelling hands the scope glob a directory name that
    is not the one on disk. This direction holds on `main` today, and it has
    to keep holding now that resolution walks to the real spelling rather than
    trusting a case-insensitive `is_file()`.
    """
    _governed_shut(vault)
    head, _sep, tail = RESTRICTED_PATH.rpartition("/Patterns/")
    miscased = f"{head}/patterns/{tail}"

    out = _filter_as_external(vault, {"entries": [{"path": miscased}]})

    assert out["entries"] == [], f"{miscased!r} crossed the release boundary"


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


@pytest.mark.parametrize("leaf", ["get", "fetch"])
@pytest.mark.parametrize("governed", [False, True], ids=["stray-tree", "governed"])
def test_direct_reads_refuse_symlinked_policy_markdown_as_missing(
    vault: Path, leaf: str, governed: bool
) -> None:
    """Containment must classify the real target without disclosing its path."""
    target = vault / "Knowledge Base" / "_Governance" / "private.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: source\n---\npolicy-tree-secret\n",
        encoding="utf-8",
    )
    if governed:
        write_scope(vault)
        write_rule(vault, ceiling=egress.LEVEL_NONE)

    rel = "Knowledge Base/Notes/ordinary-link.md"
    link = vault / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    call = (
        (lambda: commands.op_get(vault, path=rel))
        if leaf == "get"
        else (lambda: commands.op_fetch(vault, id=rel))
    )
    _reset_caches()
    with pytest.raises(ValueError) as refused:
        call()

    link.unlink()
    _reset_caches()
    with pytest.raises(ValueError) as missing:
        call()

    assert str(refused.value) == str(missing.value)
    assert str(refused.value) == f"NOT_FOUND: file does not exist: {rel}"
    assert "_Governance" not in str(refused.value)
    assert "policy-tree-secret" not in str(refused.value)


def _symlink_policy_markdown(vault: Path, rel: str) -> Path:
    target = vault / "Knowledge Base" / "_Governance" / "private.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: source\nclassification: governance\n---\npolicy-tree-secret\n",
        encoding="utf-8",
    )
    link = vault / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    return link


@pytest.mark.parametrize("leaf", ["get", "fetch"])
@pytest.mark.parametrize(
    "alias",
    [
        "Notes/review-link",
        "exomem://vault/Notes/review-link",
        "exomem://source/Notes/review-link",
    ],
    ids=["kb-shortcut", "vault-ref", "source-ref"],
)
def test_direct_reads_refuse_kb_shortcuts_to_policy_symlinks(
    vault: Path, leaf: str, alias: str
) -> None:
    link = _symlink_policy_markdown(
        vault, "Knowledge Base/Notes/review-link.md"
    )
    call = (
        (lambda: commands.op_get(vault, path=alias))
        if leaf == "get"
        else (lambda: commands.op_fetch(vault, id=alias))
    )

    with pytest.raises(ValueError) as refused:
        call()
    link.unlink()
    with pytest.raises(ValueError) as missing:
        call()

    assert str(refused.value) == str(missing.value)
    assert "_Governance" not in str(refused.value)


@pytest.mark.parametrize("leaf", ["get", "fetch"])
def test_extensionless_policy_refusal_matches_missing_file(
    vault: Path, leaf: str
) -> None:
    rel = "Knowledge Base/Notes/extensionless-link"
    link = _symlink_policy_markdown(vault, f"{rel}.md")
    call = (
        (lambda: commands.op_get(vault, path=rel))
        if leaf == "get"
        else (lambda: commands.op_fetch(vault, id=rel))
    )

    with pytest.raises(ValueError) as refused:
        call()
    link.unlink()
    with pytest.raises(ValueError) as missing:
        call()

    assert str(refused.value) == str(missing.value)
    assert str(refused.value) == f"NOT_FOUND: file does not exist: {rel}.md"


@pytest.mark.parametrize("leaf", ["get", "fetch", "frontmatter"])
def test_direct_read_uses_the_target_it_classified_when_symlink_is_swapped(
    vault: Path, leaf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ordinary = vault / "Knowledge Base" / "Notes" / "ordinary-target.md"
    ordinary.parent.mkdir(parents=True, exist_ok=True)
    ordinary.write_text(
        "---\ntype: source\nclassification: ordinary\n---\nordinary-body\n",
        encoding="utf-8",
    )
    governance = vault / "Knowledge Base" / "_Governance" / "private.md"
    governance.parent.mkdir(parents=True, exist_ok=True)
    governance.write_text(
        "---\ntype: source\nclassification: governance\n---\npolicy-tree-secret\n",
        encoding="utf-8",
    )
    rel = "Knowledge Base/Notes/swap-link.md"
    link = vault / rel
    try:
        link.symlink_to(ordinary)
    except OSError:
        pytest.skip("symlinks unavailable")

    original_resolve = Path.resolve
    swapped = False

    def _resolve_then_swap(self: Path, *args, **kwargs) -> Path:
        nonlocal swapped
        resolved = original_resolve(self, *args, **kwargs)
        if self == link and not swapped:
            link.unlink()
            link.symlink_to(governance)
            swapped = True
        return resolved

    monkeypatch.setattr(Path, "resolve", _resolve_then_swap)

    if leaf == "fetch":
        out = commands.op_fetch(vault, id=rel)
        assert "ordinary-body" in out["text"]
        assert "policy-tree-secret" not in out["text"]
    else:
        out = commands.op_get(vault, path=rel, frontmatter_only=leaf == "frontmatter")
        assert out["frontmatter"]["classification"] == "ordinary"
        assert "policy-tree-secret" not in json.dumps(out, default=str)
    assert swapped is True


@pytest.mark.parametrize("leaf", ["get", "fetch", "frontmatter"])
def test_direct_read_uses_the_prepared_ordinary_snapshot_after_target_replacement(
    vault: Path, leaf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-classification rename cannot substitute the returned bytes."""
    from exomem import get_frontmatter as get_frontmatter_module
    from exomem import get_page as get_page_module

    rel = "Knowledge Base/Notes/prepared-snapshot.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: source\nclassification: ordinary\n---\nordinary-body\n",
        encoding="utf-8",
    )
    replacement = target.with_name("prepared-snapshot-replacement.md")
    replacement.write_text(
        "---\ntype: source\nclassification: replacement\n---\nreplacement-body\n",
        encoding="utf-8",
    )

    original_refusal = commands._refuse_policy_tree_read
    replaced = False

    def _classify_then_replace(*args: object, **kwargs: object) -> None:
        nonlocal replaced
        original_refusal(*args, **kwargs)
        if not replaced:
            replacement.replace(target)
            replaced = True

    monkeypatch.setattr(commands, "_refuse_policy_tree_read", _classify_then_replace)

    if leaf == "fetch":
        out = commands.op_fetch(vault, id=rel)
        assert "ordinary-body" in out["text"]
        assert "replacement-body" not in out["text"]
    elif leaf == "get":
        out = commands.op_get(vault, path=rel)
        assert out["frontmatter"]["classification"] == "ordinary"
        assert out["body"] == "ordinary-body\n"
    else:
        prepared = get_page_module.prepare_page_read(vault, path=rel)
        commands._refuse_policy_tree_read(
            prepared.resolved_relative, missing_path=prepared.missing_path
        )
        out = get_frontmatter_module.get_frontmatter(vault, path=rel, _prepared=prepared)
        assert out.frontmatter["classification"] == "ordinary"

    assert replaced is True


def test_prepare_page_read_refuses_a_target_replaced_while_binding(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preparation refuses rather than binding bytes from a swapped leaf."""
    from exomem import get_page as get_page_module

    rel = "Knowledge Base/Notes/preparation-race.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\ntype: source\n---\nordinary-body\n", encoding="utf-8")
    replacement = target.with_name("preparation-race-replacement.md")
    replacement.write_text("---\ntype: source\n---\nreplacement-body\n", encoding="utf-8")

    original_open = get_page_module.os.open
    replaced = False

    def _replace_then_open(path: object, flags: int, *args: object) -> int:
        nonlocal replaced
        if not replaced and Path(path) == target:
            replacement.replace(target)
            replaced = True
        return original_open(path, flags, *args)

    monkeypatch.setattr(get_page_module.os, "open", _replace_then_open)

    with pytest.raises(get_page_module.GetError) as exc:
        get_page_module.prepare_page_read(vault, path=rel)

    assert exc.value.code == "UNREADABLE"
    assert replaced is True


@pytest.mark.parametrize(
    "surface",
    ["get", "fetch", "frontmatter", "get-page", "get-frontmatter"],
)
def test_direct_read_refuses_an_ordinary_alias_to_an_excluded_target(
    vault: Path, surface: str
) -> None:
    """Direct readers enforce exclusion on the resolved target as well."""
    from exomem import get_frontmatter as get_frontmatter_module
    from exomem import get_page as get_page_module

    secret = vault / "Knowledge Base/Private/secret.md"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("---\ntype: source\n---\nsecret body\n", encoding="utf-8")
    (vault / "Knowledge Base/_access.yaml").write_text(
        "excluded:\n  - Private\n", encoding="utf-8"
    )
    rel = "Knowledge Base/Notes/private-alias.md"
    alias = vault / rel
    alias.parent.mkdir(parents=True, exist_ok=True)
    try:
        alias.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable")

    error_type = {
        "get": ValueError,
        "fetch": ValueError,
        "frontmatter": ValueError,
        "get-page": get_page_module.GetError,
        "get-frontmatter": get_frontmatter_module.GetFrontmatterError,
    }[surface]

    with pytest.raises(error_type) as refused:
        _read_alias_surface(vault, path=rel, surface=surface)
    alias.unlink()
    with pytest.raises(error_type) as missing:
        _read_alias_surface(vault, path=rel, surface=surface)

    assert str(refused.value) == str(missing.value)


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


# --------------------------------------------------------------------------
# A scope may deny audiences it does not name (add-default-deny-scope-cap)
#
# Wave 0's finding, at the surfaces: audiences are matched by EXACT id, and
# for a non-OAuth MCP client the subject derives from the bearer credential —
# so rotating a credential mints an audience id no rule names, and the vault
# falls open to it. These pin the closed default end to end.
# --------------------------------------------------------------------------

DECLARED_VIDEO = "Knowledge Base/Notes/Patterns/board-briefing.mp4"


def _audience_for(credential: str) -> str:
    """The audience id a bearer credential resolves to, as the MCP surface
    mints it — the thing a rotation changes."""
    from exomem.governance.principal import normalize_audience

    return normalize_audience(subject=credential, issuer="mcp-bearer")


def _as(audience: str) -> RequestPrincipal:
    return RequestPrincipal(audience_id=audience, surface="mcp")


def test_a_declared_scope_denies_an_unnamed_audience_end_to_end(vault: Path) -> None:
    """No rule document at all: the scope alone closes the item."""
    write_scope(vault, default_deny=True)
    _reset_caches()
    with request_scope(_external()):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_get(vault, path=RESTRICTED_PATH)


def test_the_same_scope_without_the_declaration_still_releases(vault: Path) -> None:
    """The control for the test above — the change is opt-in, so an identical
    scope carrying no declaration keeps today's full release."""
    write_scope(vault)
    _reset_caches()
    with request_scope(_external()):
        page = commands.op_get(vault, path=RESTRICTED_PATH)
    assert "Kill switch" in page["body"]


def test_a_rotated_credential_minting_an_unnamed_audience_id_is_denied(
    vault: Path,
) -> None:
    """Spec: a newly minted audience id is denied by default.

    The rule names the audience the ORIGINAL credential resolves to. Rotating
    the credential produces a different id that appears in no document — the
    precondition the owner cannot enforce and will not remember."""
    before = _audience_for("bearer-token-v1")
    after = _audience_for("bearer-token-v2")
    never_named = _audience_for("some-assistant-that-was-never-authored")
    assert len({before, after, never_named}) == 3

    write_scope(vault, default_deny=True)
    write_rule(vault, ceiling=egress.LEVEL_FULL, audience=before)
    _reset_caches()

    with request_scope(_as(before)):
        page = commands.op_get(vault, path=RESTRICTED_PATH)
    assert "Kill switch" in page["body"]

    def _denied(audience: str) -> str:
        _reset_caches()
        with request_scope(_as(audience)):
            with pytest.raises(ValueError) as excinfo:
                commands.op_get(vault, path=RESTRICTED_PATH)
        return str(excinfo.value)

    # …and the rotated id is denied identically to one that was never authored.
    assert _denied(after) == _denied(never_named)
    assert _denied(after) == f"NOT_FOUND: file does not exist: {RESTRICTED_PATH}"


def test_a_rotated_credential_falls_open_without_the_declaration(vault: Path) -> None:
    """The defect the declaration exists to close, pinned as the control: the
    same policy WITHOUT the declaration serves the rotated credential in full."""
    before = _audience_for("bearer-token-v1")
    after = _audience_for("bearer-token-v2")
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE, audience=before)
    _reset_caches()
    with request_scope(_as(after)):
        page = commands.op_get(vault, path=RESTRICTED_PATH)
    assert "Kill switch" in page["body"]


def test_a_grant_still_reaches_an_unnamed_audience_in_a_declared_scope(
    vault: Path,
) -> None:
    """Spec: a grant still raises above the default, with no special case."""
    audience = _audience_for("bearer-token-v2")
    write_scope(vault, default_deny=True)
    grant = _gov_dir(vault) / "grants" / "rotated.yaml"
    grant.parent.mkdir(parents=True, exist_ok=True)
    grant.write_text(
        f"governance_version: 1\nid: {GRANT_ID}\nkind: standing\n"
        f'scope_ids: ["{SCOPE_ID}"]\naudience: {audience}\n'
        f"ceiling: {egress.LEVEL_FULL}\n",
        encoding="utf-8",
    )
    _reset_caches()
    with request_scope(_as(audience)):
        page = commands.op_get(vault, path=RESTRICTED_PATH)
    assert "Kill switch" in page["body"]


def test_the_owner_still_reads_a_declared_scope(vault: Path) -> None:
    """Spec: the owner reads a declared scope at full release. A scope the
    owner cannot read is a vault that has lost its own contents."""
    write_scope(vault, default_deny=True)
    _reset_caches()
    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        page = commands.op_get(vault, path=RESTRICTED_PATH)
    assert "Kill switch" in page["body"]


def test_an_ungoverned_vault_is_untouched_by_the_declaration(vault: Path) -> None:
    """No governance tree still means the empty fast path."""
    baseline = commands.op_get(vault, path=RESTRICTED_PATH)
    _reset_caches()
    with request_scope(_external()):
        governed = commands.op_get(vault, path=RESTRICTED_PATH)
    assert governed == baseline


#: Deterministic stand-in for the fixture note, so the two probes below differ
#: only in whether the item exists.
_DECLARED_NOTE = (
    "---\ntype: pattern\ntitle: Kill switch for risky releases\n---\n"
    "A kill switch limits the blast radius of a risky release.\n"
)

#: Corpus-statistics fields a withheld item still moves on the items that
#: SURVIVE it: it displaces their ranks and counts toward their in-degree.
#: This channel predates the change and is identical for an authored
#: `ceiling: 0` rule — pinned by the differential test below rather than
#: assumed here, so excluding it is a proven property and not hand-waving.
_RANK_DERIVED_SIGNALS = ("bm25_rank", "keyword_rank", "vector_rank", "graph_in_degree")


def test_spec_names_the_known_preexisting_relevance_ranking_channel() -> None:
    spec = (
        Path(__file__).resolve().parents[1]
        / "openspec/changes/archive/2026-08-13-add-default-deny-scope-cap/specs/governance-kernel/spec.md"
    ).read_text(encoding="utf-8")
    scenario = spec.split(
        "#### Scenario: an audience no rule names receives nothing", 1
    )[1].split("#### Scenario:", 1)[0]

    assert all(signal in scenario for signal in _RANK_DERIVED_SIGNALS)
    assert "known pre-existing channel" in scenario
    assert "tracked separately" in scenario


def _strip_rank_signals(payload):
    hits = payload["hits"] if isinstance(payload, dict) else payload
    for hit in hits if isinstance(hits, list) else []:
        signals = hit.get("signals") if isinstance(hit, dict) else None
        if isinstance(signals, dict):
            for key in _RANK_DERIVED_SIGNALS:
                signals.pop(key, None)
    return payload


def _probe_surfaces_present_then_absent(
    vault: Path, *, keep_rank_signals: bool = False
) -> tuple[dict[str, str], dict[str, str]]:
    """Ask every surface for the same two paths twice: present, then deleted.

    Returns `(present, absent)` keyed by surface. The caller supplies the
    policy, so the only thing that varies between the two dicts is whether the
    items exist on disk.
    """
    md = vault / RESTRICTED_PATH
    video = vault / DECLARED_VIDEO
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(_DECLARED_NOTE, encoding="utf-8")
    # The bytes are never reached: the release gate refuses before extraction.
    video.write_bytes(PNG_1X1)

    surfaces = {
        "get": lambda: commands.op_get(vault, path=RESTRICTED_PATH),
        "fetch": lambda: commands.op_fetch(vault, id=RESTRICTED_PATH),
        "read_media": lambda: commands.op_get_video_frames(vault, path=DECLARED_VIDEO),
        "recall": lambda: commands.op_find(
            vault, query="kill switch risky releases", limit=10
        ),
        "graph": lambda: commands.op_graph_context(vault, path=RESTRICTED_PATH),
    }

    def _probe(name: str, call) -> str:
        _reset_caches()
        with request_scope(_external()):
            try:
                payload = call()
            except ValueError as error:
                return f"raised:{error}"
        if name == "recall" and not keep_rank_signals:
            payload = _strip_rank_signals(payload)
        return f"ok:{json.dumps(payload, default=str, sort_keys=True)}"

    present = {name: _probe(name, call) for name, call in surfaces.items()}
    md.unlink()
    video.unlink()
    absent = {name: _probe(name, call) for name, call in surfaces.items()}
    return present, absent


def test_a_default_denied_item_matches_missing_except_known_ranking_signals(
    vault: Path,
) -> None:
    """Spec: the item matches a missing one outside the named ranking channel.

    Same-input/varied-condition: each surface is asked for ONE path twice —
    once while it exists and is denied by the declaration, once after deleting
    it. The caller controls the input, so any non-ranking difference would be
    a new existence oracle introduced by the declaration.

    Comparing two DIFFERENT withheld paths cannot isolate that bit: a response
    legitimately echoes whichever path the caller named, so differing inputs
    are expected to differ. A prior round of this work shipped an existence
    oracle for exactly that reason.
    """
    write_scope(vault, default_deny=True)
    present, absent = _probe_surfaces_present_then_absent(vault)

    divergent = {
        name: (present[name], absent[name])
        for name in present
        if present[name] != absent[name]
    }
    assert not divergent, (
        "EXISTENCE ORACLE: a default-denied item answers differently from a "
        f"missing one on surface(s): {sorted(divergent)}"
    )
    # `recall` is the one surface the caller did not name the item on, so the
    # item must not come back as a hit there.
    #
    # Deliberately NOT "the stem appears nowhere in the payload": a PERMITTED
    # note's own prose may link to the closed one, and that excerpt reads the
    # same whether the target exists or not — the assertion above already
    # proved it byte-identical, so it carries no existence bit. Reference
    # FIELDS (provenance, relations, graph seeds) are stripped, which
    # `test_a_withheld_path_is_recognised_in_every_reference_form` covers.
    # The probe deletes the item on its way out, so restore it before asking.
    (vault / RESTRICTED_PATH).write_text(_DECLARED_NOTE, encoding="utf-8")
    _reset_caches()
    with request_scope(_external()):
        hits = commands.op_find(vault, query="kill switch risky releases", limit=10)
    returned = [hit.get("path") for hit in (hits["hits"] if isinstance(hits, dict) else hits)]
    assert (vault / RESTRICTED_PATH).is_file()
    assert RESTRICTED_PATH not in returned


def test_the_declaration_is_no_worse_than_the_existing_ceiling_0_primitive(
    vault: Path,
) -> None:
    """The assertion this change owns: it is no worse than the old primitive.

    `_RANK_DERIVED_SIGNALS` is excluded from the test above because a withheld
    item still displaces the ranks of the items that survive it, and still
    counts toward their `graph_in_degree` — a corpus-statistics channel that
    predates this change and is identical for an explicit `ceiling: 0` rule
    (verified against pristine `main`). This differential keeps the declaration
    no worse than that existing primitive: every surface, INCLUDING the named
    signals, answers byte-identically whether the item is closed by the
    declaration or by an authored ceiling-0 rule.
    """
    write_scope(vault, default_deny=True)
    declared_present, declared_absent = _probe_surfaces_present_then_absent(
        vault, keep_rank_signals=True
    )

    # Same vault, same items, closed the already-supported way instead.
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    authored_present, authored_absent = _probe_surfaces_present_then_absent(
        vault, keep_rank_signals=True
    )

    assert declared_present == authored_present
    assert declared_absent == authored_absent


def test_a_broad_undeclared_scope_cannot_reopen_a_declared_one(vault: Path) -> None:
    """Spec: one declared scope denies across an overlapping undeclared scope.

    The attack this closes: if membership in ANY undeclared scope re-opened the
    item, the declaration would be defeatable by authoring a broad scope
    alongside it — and authoring a broad scope is the ordinary thing an owner
    does next. Proven through real membership resolution, not just the lattice.
    """
    write_scope(vault, default_deny=True)  # Notes/Patterns/** , declared
    broad = _gov_dir(vault) / "scopes" / "everything.yaml"
    broad.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB2\nname: Everything\n"
        'paths: ["Notes/**"]\n',
        encoding="utf-8",
    )
    _reset_caches()

    from exomem.governance import policy as policy_module

    pol = policy_module.load(vault)
    decision = egress._decide_path(
        vault,
        RESTRICTED_PATH,
        policy=pol,
        audience=EXTERNAL,
        purpose=None,
        grants_hash=egress._grants_hash(pol),
    )
    assert decision is not None
    # Both scopes really do contain the item — otherwise the test proves nothing.
    assert len(decision.scope_ids) == 2
    assert decision.level == 0
    assert decision.default_deny_scope_ids == (SCOPE_ID,)

    _reset_caches()
    with request_scope(_external()):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_get(vault, path=RESTRICTED_PATH)


def test_owner_explain_exposes_per_scope_contributions_only_to_owner(vault: Path) -> None:
    from exomem.governance.inspection import inspect_operation

    write_scope(vault, default_deny=True)
    second_scope = "01ARZ3NDEKTSV4RRFFQ69G5FC2"
    (_gov_dir(vault) / "scopes" / "second.yaml").write_text(
        f"governance_version: 1\nid: {second_scope}\nname: Second\n"
        f'paths: ["{PATTERNS_GLOB}"]\ndefault_deny: true\n',
        encoding="utf-8",
    )
    write_rule(
        vault,
        ceiling=5,
        extra="options:\n  constraint: reviewed constraint\n",
    )
    (_gov_dir(vault) / "rules" / "cap.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC3\n"
        f'scope_ids: ["{SCOPE_ID}"]\naudience: {EXTERNAL}\nkind: org_cap\n'
        "ceiling: 4\noptions:\n  constraint: reviewed constraint\n",
        encoding="utf-8",
    )
    grants = _gov_dir(vault) / "grants"
    grants.mkdir(parents=True, exist_ok=True)
    (grants / "second.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC4\nkind: standing\n"
        f'scope_ids: ["{second_scope}"]\naudience: {EXTERNAL}\nceiling: 3\n',
        encoding="utf-8",
    )
    _reset_caches()

    owner_result = inspect_operation(
        vault,
        "explain",
        principal=owner_principal(),
        audience=EXTERNAL,
        path=RESTRICTED_PATH,
    )
    non_owner_result = inspect_operation(
        vault,
        "explain",
        principal=_external(),
        audience=EXTERNAL,
        path=RESTRICTED_PATH,
    )

    assert owner_result["effective_ceiling"] == 1
    assert owner_result["scope_contributions"] == [
        {
            "scope_id": SCOPE_ID,
            "purpose_branch": "neutral",
            "standing_floor": 5,
            "default_deny_supplied_floor": False,
            "standing_rule_ids": [RULE_ID],
            "grant_ids": [],
            "grant_ceiling": None,
            "grant_contribution": 5,
            "organization_cap_ids": ["01ARZ3NDEKTSV4RRFFQ69G5FC3"],
            "organization_cap": 4,
            "option_values": {"constraint": "reviewed constraint"},
            "option_ambiguities": [],
            "final_ceiling": 4,
        },
        {
            "scope_id": second_scope,
            "purpose_branch": "neutral",
            "standing_floor": 0,
            "default_deny_supplied_floor": True,
            "standing_rule_ids": [],
            "grant_ids": ["01ARZ3NDEKTSV4RRFFQ69G5FC4"],
            "grant_ceiling": 3,
            "grant_contribution": 3,
            "organization_cap_ids": [],
            "organization_cap": 6,
            "option_values": {},
            "option_ambiguities": [],
            "final_ceiling": 3,
        },
    ]
    assert set(non_owner_result) == {
        "enabled",
        "effective_ceiling",
        "rule_ids",
        "participating_chain",
    }


def test_owner_explain_labels_purpose_branches_before_conservative_meet(
    vault: Path,
) -> None:
    from exomem.governance.inspection import inspect_operation

    write_scope(vault)
    rules = _gov_dir(vault) / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "declared.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC5\n"
        f'scope_ids: ["{SCOPE_ID}"]\naudience: {EXTERNAL}\nceiling: 3\n'
        "purpose: audit\npurpose_condition: matches\n"
        "options:\n  abstract: declared abstract\n",
        encoding="utf-8",
    )
    (rules / "undeclared.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC6\n"
        f'scope_ids: ["{SCOPE_ID}"]\naudience: {EXTERNAL}\nceiling: 3\n'
        "purpose: audit\npurpose_condition: outside\n",
        encoding="utf-8",
    )
    _reset_caches()

    result = inspect_operation(
        vault,
        "explain",
        principal=owner_principal(),
        audience=EXTERNAL,
        purpose="audit",
        path=RESTRICTED_PATH,
    )

    assert result["effective_ceiling"] == 2
    assert [row["purpose_branch"] for row in result["scope_contributions"]] == [
        "declared",
        "undeclared",
    ]
    assert [row["final_ceiling"] for row in result["scope_contributions"]] == [
        3,
        3,
    ]
    assert result["scope_contributions"][0]["option_values"] == {
        "abstract": "declared abstract"
    }
    assert result["scope_contributions"][1]["option_values"] == {}
