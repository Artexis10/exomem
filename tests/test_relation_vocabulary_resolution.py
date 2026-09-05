from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exomem import memory_schema, relation_registry, relation_vocabulary


def _proposal(**extensions):
    return {"schema_version": 1, "extensions": extensions}


def _extension(
    description: str,
    *,
    aliases: list[str] | None = None,
    status: str = "active",
    replaced_by: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "parent": "relates_to",
        "description": description,
        "direction": "directed",
    }
    if aliases:
        result["aliases"] = aliases
    if status != "active":
        result["status"] = status
    if replaced_by:
        result["replaced_by"] = replaced_by
    return result


def test_resolver_returns_complete_core_and_honest_outcomes() -> None:
    result = relation_vocabulary.resolve_relation(
        relation_registry.core_registry(), query="a child belongs to its parent"
    )

    vocabulary = {item["key"]: item for item in result["core_vocabulary"]}
    assert len(vocabulary) == 28
    assert vocabulary["part_of"]["inverse"] == "contains"
    assert result["honest_outcomes"] == {
        "relates_to": "available when a meaningful generic connection is justified",
        "no_edge": "available when no durable relationship is established",
    }
    assert result["selected_relation"] is None
    assert result["proposed_relation"] is None


def test_resolver_refuses_missing_intent() -> None:
    with pytest.raises(ValueError, match="RELATION_QUERY_REQUIRED"):
        relation_vocabulary.resolve_relation(relation_registry.core_registry())


def test_resolver_refuses_invalid_limit() -> None:
    for limit in (0, 65, 1_000_000):
        with pytest.raises(ValueError, match="RELATION_LIMIT_INVALID"):
            relation_vocabulary.resolve_relation(
                relation_registry.core_registry(), query="intent", limit=limit
            )


def test_indexed_observations_page_in_sql_with_exact_offset_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph

    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE graph_edges ("
        "registry_status TEXT, raw_relation TEXT, source_path TEXT, source_anchor TEXT)"
    )
    connection.executemany(
        "INSERT INTO graph_edges VALUES ('unregistered', ?, ?, NULL)",
        [
            (f"relation_{index:03d}", f"Knowledge Base/Notes/{index:03d}.md")
            for index in range(257)
        ],
    )
    fetched_row_counts: list[int] = []
    statements: list[str] = []

    class Cursor:
        def __init__(self, inner: sqlite3.Cursor) -> None:
            self.inner = inner

        def fetchone(self):  # noqa: ANN201
            return self.inner.fetchone()

        def fetchall(self):  # noqa: ANN201
            rows = self.inner.fetchall()
            fetched_row_counts.append(len(rows))
            return rows

    class Snapshot:
        def create_function(
            self,
            name: str,
            num_params: int,
            func,
            *,
            deterministic: bool = False,
        ) -> None:  # noqa: ANN001
            connection.create_function(
                name, num_params, func, deterministic=deterministic
            )

        def execute(self, sql: str, parameters=()):  # noqa: ANN001, ANN201
            statements.append(sql)
            return Cursor(connection.execute(sql, parameters))

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_open_read_snapshot",
        lambda _self: Snapshot(),
    )

    page = memory_schema.indexed_relation_observations(tmp_path, limit=3, offset=129)

    assert page == {
        "items": [
            {
                "raw_relation": f"relation_{index:03d}",
                "registry_status": "unregistered",
                "count": 1,
                "examples": [
                    {
                        "path": f"Knowledge Base/Notes/{index:03d}.md",
                        "anchor": None,
                    }
                ],
            }
            for index in range(129, 132)
        ],
        "total": 257,
        "returned": 3,
        "omitted": 254,
        "offset": 129,
    }
    assert fetched_row_counts
    assert max(fetched_row_counts) <= 15
    assert any("LIMIT" in statement.upper() for statement in statements)


def test_indexed_observations_use_canonical_unbounded_separator_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph

    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE graph_edges ("
        "registry_status TEXT, raw_relation TEXT, source_path TEXT, "
        "source_anchor TEXT, metadata TEXT)"
    )
    connection.executemany(
        "INSERT INTO graph_edges VALUES ('unregistered', ?, ?, ?, ?)",
        [
            ("applies-to", "b.md", "second", None),
            ("applies-----to", "a.md", None, None),
            ("applies to", "a.md", None, None),
            ("applies_to", "c.md", "third", None),
            ("applies_to", "d.md", None, None),
            ("applies-to", "e.md", None, None),
            ("applies to", "f.md", None, None),
            (
                "applies_____to",
                "g.md",
                None,
                '{"line":"- applies-----to: [[target]]"}',
            ),
            (
                "applies__to",
                "h.md",
                None,
                '{"line":"- applies__to: [[target]]"}',
            ),
        ],
    )
    registrations: list[tuple[str, int, object, bool]] = []
    statements: list[str] = []

    class Snapshot:
        def create_function(
            self,
            name: str,
            num_params: int,
            func,
            *,
            deterministic: bool = False,
        ) -> None:  # noqa: ANN001
            registrations.append((name, num_params, func, deterministic))
            connection.create_function(
                name, num_params, func, deterministic=deterministic
            )

        def execute(self, sql: str, parameters=()):  # noqa: ANN001, ANN201
            statements.append(sql)
            return connection.execute(sql, parameters)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_open_read_snapshot",
        lambda _self: Snapshot(),
    )

    page = memory_schema.indexed_relation_observations(
        tmp_path, raw_relations=["applies-----to"], limit=8, offset=0
    )

    assert page == {
        "items": [
            {
                "raw_relation": "applies_to",
                "registry_status": "unregistered",
                "count": 8,
                "examples": [
                    {"path": "a.md", "anchor": None},
                    {"path": "b.md", "anchor": "second"},
                    {"path": "c.md", "anchor": "third"},
                    {"path": "d.md", "anchor": None},
                    {"path": "e.md", "anchor": None},
                ],
            }
        ],
        "total": 1,
        "returned": 1,
        "omitted": 0,
        "offset": 0,
    }
    double_underscore = memory_schema.indexed_relation_observations(
        tmp_path, raw_relations=["applies__to"], limit=8, offset=0
    )

    assert double_underscore is not None
    assert double_underscore["total"] == 1
    assert double_underscore["items"][0]["raw_relation"] == "applies__to"
    assert double_underscore["items"][0]["count"] == 1
    assert len(registrations) == 2
    name, num_params, normalizer, deterministic = registrations[0]
    assert (name, num_params, deterministic) == (
        "exomem_normalize_relation",
        2,
        True,
    )
    assert {
        normalizer(value, None)
        for value in ("Applies-To", "applies-----to", "applies to", "applies_to")
    } == {"applies_to"}
    assert normalizer(
        "applies_____to", "- applies-----to: [[target]]"
    ) == "applies_to"
    assert normalizer("applies__to", "- applies__to: [[target]]") == "applies__to"
    assert statements
    assert all("replace(" not in statement.lower() for statement in statements)


def test_resolver_refuses_bad_continuation() -> None:
    with pytest.raises(ValueError, match="RELATION_CONTINUATION_INVALID"):
        relation_vocabulary.resolve_relation(
            relation_registry.core_registry(),
            query="intent",
            continuation="not-a-token",
        )


def test_exact_aliases_survive_bounded_extension_pages() -> None:
    registry = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "vault.first": _extension("First", aliases=["first"]),
                "vault.match": _extension("Exact match", aliases=["wanted"]),
                "vault.third": _extension("Third", aliases=["third"]),
            }
        )
    )

    result = relation_vocabulary.resolve_relation(
        registry, requested_relation="wanted", limit=1
    )

    assert result["extensions"] == {
        "total": 3,
        "returned": 1,
        "omitted": 2,
        "truncated": True,
        "continuation": "extensions:1",
    }
    assert result["exact_matches"] == [
        {
            "match": "alias",
            "requested_relation": "wanted",
            "canonical": "vault.match",
            "parent": "relates_to",
            "family": "relation",
            "description": "Exact match",
            "direction": "directed",
            "aliases": ["wanted"],
            "status": "active",
            "immediate_replacement": None,
            "terminal_replacement": None,
            "predecessors": [],
        }
    ]


def test_resolver_continuations_page_extensions_and_observations_independently() -> None:
    registry = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "vault.first": _extension("First"),
                "vault.second": _extension("Second"),
                "vault.third": _extension("Third"),
            }
        )
    )
    observations = [
        {"raw_relation": "first_unknown", "count": 3},
        {"raw_relation": "second_unknown", "count": 2},
    ]

    first = relation_vocabulary.resolve_relation(
        registry, query="unrelated", limit=1, observations=observations
    )
    second_extension = relation_vocabulary.resolve_relation(
        registry,
        query="unrelated",
        limit=1,
        observations=observations,
        continuation=first["extensions"]["continuation"],
    )
    second_observation = relation_vocabulary.resolve_relation(
        registry,
        query="unrelated",
        limit=1,
        observations=observations,
        continuation=first["observations"]["continuation"],
    )

    assert [item["canonical"] for item in first["candidates"]] == ["vault.first"]
    assert [item["canonical"] for item in second_extension["candidates"]] == [
        "vault.second"
    ]
    assert second_extension["extensions"] == {
        "total": 3,
        "returned": 1,
        "omitted": 2,
        "truncated": True,
        "continuation": "extensions:2",
    }
    assert second_observation["unregistered_pressure"] == [
        {"raw_relation": "second_unknown", "count": 2}
    ]
    assert second_observation["observations"] == {
        "total": 2,
        "returned": 1,
        "omitted": 1,
        "truncated": False,
        "continuation": None,
    }


def test_survivor_and_deprecated_exact_matches_expose_directed_history() -> None:
    registry = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "vault.old": _extension(
                    "Old narrow meaning",
                    aliases=["old"],
                    status="deprecated",
                    replaced_by="relates_to",
                )
            }
        )
    )

    survivor = relation_vocabulary.resolve_relation(
        registry, requested_relation="relates_to", limit=1
    )["exact_matches"][0]
    historical = relation_vocabulary.resolve_relation(
        registry, requested_relation="old", limit=1
    )["exact_matches"][0]

    assert survivor["canonical"] == "relates_to"
    assert survivor["predecessors"] == ["vault.old"]
    assert historical["canonical"] == "vault.old"
    assert historical["immediate_replacement"] == "relates_to"
    assert historical["terminal_replacement"] == "relates_to"
    assert historical["predecessors"] == []


def test_resolver_reports_bounded_unregistered_pressure_without_selecting_specificity() -> None:
    result = relation_vocabulary.resolve_relation(
        relation_registry.core_registry(),
        query="nearby topic",
        limit=1,
        observations=[
            {"raw_relation": "custom_link", "count": 5, "examples": [{"path": "a.md"}]},
            {"raw_relation": "other_link", "count": 2, "examples": [{"path": "b.md"}]},
        ],
    )

    assert result["unregistered_pressure"] == [
        {"raw_relation": "custom_link", "count": 5, "examples": [{"path": "a.md"}]}
    ]
    assert result["observations"] == {
        "total": 2,
        "returned": 1,
        "omitted": 1,
        "truncated": True,
        "continuation": "observations:1",
    }
    assert result["selected_relation"] is None


def test_resolver_ranks_decomposed_lexical_evidence_without_selecting_it() -> None:
    registry = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "vault.a_policy": _extension("Policy"),
                "vault.z_policy_applies_case": _extension("Policy applies to a case"),
            }
        )
    )

    result = relation_vocabulary.resolve_relation(
        registry, query="policy applies case", limit=2
    )

    assert [item["canonical"] for item in result["candidates"]] == [
        "vault.z_policy_applies_case",
        "vault.a_policy",
    ]
    specific = result["candidates"][0]
    canonical_evidence = next(
        item for item in specific["evidence"] if item["source"] == "canonical"
    )
    assert canonical_evidence["terms"] == ["applies", "case", "policy"]
    assert result["selected_relation"] is None


def test_resolver_isolated_between_vault_registries(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    relation_registry.save_registry(
        left, _proposal(**{"left.applies": _extension("Left", aliases=["applies_to"])})
    )
    relation_registry.save_registry(
        right, _proposal(**{"right.applies": _extension("Right", aliases=["applies_to"])})
    )

    left_result = relation_vocabulary.resolve_relation(
        relation_registry.load_registry(left), requested_relation="applies_to"
    )
    right_result = relation_vocabulary.resolve_relation(
        relation_registry.load_registry(right), requested_relation="applies_to"
    )

    assert left_result["exact_matches"][0]["canonical"] == "left.applies"
    assert right_result["exact_matches"][0]["canonical"] == "right.applies"


def test_proposal_requires_reviewed_semantics_and_uses_clean_alias() -> None:
    registry = relation_registry.core_registry()
    incomplete = relation_registry.propose_extension(registry, requested_label="applies_to")
    assert {finding["code"] for finding in incomplete["findings"]} == {
        "incomplete_proposal"
    }


def test_proposal_reports_invalid_semantics_and_registry_collisions() -> None:
    registry = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "vault.existing": _extension(
                    "Existing meaning", aliases=["existing_alias"]
                )
            }
        )
    )

    invalid = relation_registry.propose_extension(
        registry,
        requested_label="existing_alias",
        parent="not_core",
        description="Reviewed description",
        direction="sideways",
        namespace="bad.namespace",
        aliases=["bad alias!"],
    )

    assert {finding["code"] for finding in invalid["findings"]} == {
        "collision",
        "invalid_alias",
        "invalid_direction",
        "invalid_key",
        "invalid_parent",
    }

    proposal = relation_registry.propose_extension(
        registry,
        requested_label="applies_to",
        parent="relates_to",
        description="A policy applies to a case.",
        direction="directed",
    )
    assert proposal["delta"] == {
        "upsert": {
            "vault.applies_to": {
                "parent": "relates_to",
                "description": "A policy applies to a case.",
                "direction": "directed",
                "aliases": ["applies_to"],
            }
        }
    }


def test_relation_proposal_carries_complete_bounded_duplicate_evidence() -> None:
    registry = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "vault.first": _extension("First"),
                "vault.applies": _extension(
                    "Existing policy applicability", aliases=["applies_to"]
                ),
                "vault.third": _extension("Third"),
            }
        )
    )

    result = relation_vocabulary.propose_relation(
        registry,
        requested_label="applies_to",
        parent="relates_to",
        description="A policy applies to a case.",
        direction="directed",
        limit=1,
        observations=[{"raw_relation": "applies_on", "count": 4}],
    )

    evidence = result["duplicate_evidence"]
    assert evidence["exact_matches"][0]["canonical"] == "vault.applies"
    assert evidence["extensions"] == {
        "total": 3,
        "returned": 1,
        "omitted": 2,
        "truncated": True,
        "continuation": "extensions:1",
    }
    assert evidence["observations"] == {
        "total": 1,
        "returned": 1,
        "omitted": 0,
        "truncated": False,
        "continuation": None,
    }
    assert result["expected_hash"] == registry.extension_hash
    assert result["delta"]["upsert"]["vault.applies_to"]["aliases"] == [
        "applies_to"
    ]


def test_relation_proposal_checks_canonical_and_every_alias_outside_bounded_page() -> None:
    registry = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "vault.aaa": _extension("Foo"),
                "vault.foo": _extension("Canonical-only duplicate"),
                "vault.alias_one": _extension(
                    "First alias duplicate", aliases=["canonical_alias"]
                ),
                "vault.other": _extension(
                    "Alias duplicate", aliases=["alternate"]
                ),
            }
        )
    )

    result = relation_vocabulary.propose_relation(
        registry,
        requested_label="foo",
        parent="relates_to",
        description="Proposed meaning",
        direction="directed",
        aliases=["canonical_alias", "alternate"],
        query="unrelated",
        limit=1,
    )

    evidence = result["duplicate_evidence"]
    assert [item["canonical"] for item in evidence["candidates"]] == ["vault.aaa"]
    assert [item["canonical"] for item in evidence["exact_matches"]] == [
        "vault.foo",
        "vault.alias_one",
        "vault.other",
    ]
    assert evidence["extensions"]["truncated"] is True


def test_relation_proposal_emits_only_supplied_approved_optional_semantics() -> None:
    registry = relation_registry.core_registry()

    result = relation_vocabulary.propose_relation(
        registry,
        requested_label="applies_to",
        parent="relates_to",
        description="A policy applies to a case.",
        direction="directed",
        inverse="contains",
        origins=["markdown_relation"],
        source_kinds=["file"],
        target_kinds=["decision"],
        projects=["atlas"],
        page_types=["insight"],
    )

    value = result["delta"]["upsert"]["vault.applies_to"]
    assert value == {
        "parent": "relates_to",
        "description": "A policy applies to a case.",
        "direction": "directed",
        "aliases": ["applies_to"],
        "inverse": "contains",
        "origins": ["markdown_relation"],
        "source_kinds": ["file"],
        "target_kinds": ["decision"],
        "scope": {"projects": ["atlas"], "page_types": ["insight"]},
    }
    parsed = relation_registry.load_registry(
        proposal={"schema_version": 1, "extensions": result["delta"]["upsert"]}
    )
    assert parsed.findings == ()
    definition = parsed.extensions["vault.applies_to"]
    assert definition.inverse == "contains"
    assert definition.origins == frozenset({"markdown_relation"})
    assert definition.source_kinds == frozenset({"file"})
    assert definition.target_kinds == frozenset({"decision"})
    assert definition.projects == frozenset({"atlas"})
    assert definition.page_types == frozenset({"insight"})

    minimal = relation_registry.propose_extension(
        registry,
        requested_label="minimal",
        parent="relates_to",
        description="Minimal reviewed meaning.",
        direction="directed",
    )["delta"]["upsert"]["vault.minimal"]
    assert not {
        "inverse",
        "origins",
        "source_kinds",
        "target_kinds",
        "scope",
    } & minimal.keys()


def test_delta_merge_preserves_unrelated_definitions_and_converges_stale_hash() -> None:
    current = _proposal(
        **{
            "vault.keep": _extension("Keep", aliases=["keep"]),
            "vault.change": _extension("Change", aliases=["change"]),
        }
    )
    merged = relation_registry.merge_extension_delta(
        current,
        {"upsert": {"vault.change": {"aliases": ["changed"]}}},
    )

    assert merged["extensions"]["vault.keep"] == current["extensions"]["vault.keep"]
    assert merged["extensions"]["vault.change"]["aliases"] == ["change", "changed"]
    with pytest.raises(ValueError, match="STALE_RELATION_REGISTRY"):
        relation_registry.require_current_hash("new", "old")


@pytest.mark.parametrize(
    ("delta", "error"),
    [
        (
            {"upsert": {"vault.current": {"description": "Changed"}}},
            "IMMUTABLE_RELATION_MEANING",
        ),
        (
            {"upsert": {"vault.current": {"aliases": []}}},
            "IMMUTABLE_RELATION_ALIASES",
        ),
        (
            {"upsert": {"vault.old": {"replaced_by": "relates_to"}}},
            "IMMUTABLE_REPLACEMENT",
        ),
        (
            {"upsert": {"vault.old": {"status": "active"}}},
            "IMMUTABLE_REPLACEMENT",
        ),
        (
            {"upsert": {"vault.current": {"unexpected": True}}},
            "INVALID_RELATION_DELTA",
        ),
        ({"remove": ["vault.current"]}, "INVALID_RELATION_DELTA"),
        ({"upsert": []}, "INVALID_RELATION_DELTA"),
        ({"deprecate": []}, "INVALID_RELATION_DELTA"),
        (
            {"upsert": {"vault.current": {"aliases": "not-a-list"}}},
            "INVALID_RELATION_DELTA",
        ),
    ],
)
def test_delta_refuses_invalid_or_discontinuous_changes(
    delta: dict[str, object], error: str
) -> None:
    current = _proposal(
        **{
            "vault.old": _extension(
                "Old", status="deprecated", replaced_by="vault.current"
            ),
            "vault.current": _extension("Current", aliases=["current"]),
        }
    )

    with pytest.raises(ValueError, match=error):
        relation_registry.merge_extension_delta(current, delta)


def test_multihop_replacements_expose_terminal_and_predecessor_closure() -> None:
    proposal = _proposal(
        **{
            "vault.old": _extension(
                "Old", status="deprecated", replaced_by="vault.middle"
            ),
            "vault.middle": _extension(
                "Middle", status="deprecated", replaced_by="vault.current"
            ),
            "vault.current": _extension("Current"),
        }
    )
    registry = relation_registry.load_registry(proposal=proposal)

    assert registry.terminal_replacement("vault.old") == "vault.current"
    assert registry.predecessors("vault.current") == frozenset({"vault.old", "vault.middle"})
    exact = relation_vocabulary.resolve_relation(registry, requested_relation="vault.old")
    assert exact["exact_matches"][0]["immediate_replacement"] == "vault.middle"
    assert exact["exact_matches"][0]["terminal_replacement"] == "vault.current"
    candidate = next(
        item
        for item in relation_vocabulary.resolve_relation(registry, query="old")["candidates"]
        if item["canonical"] == "vault.old"
    )
    assert candidate["immediate_replacement"] == "vault.middle"
    assert candidate["terminal_replacement"] == "vault.current"


def test_later_intermediate_deprecation_preserves_immediate_history() -> None:
    current = _proposal(
        **{
            "vault.old": _extension(
                "Old", status="deprecated", replaced_by="vault.middle"
            ),
            "vault.middle": _extension("Middle"),
            "vault.current": _extension("Current"),
        }
    )

    merged = relation_registry.merge_extension_delta(
        current, {"deprecate": {"vault.middle": "vault.current"}}
    )
    registry = relation_registry.load_registry(proposal=merged)

    assert merged["extensions"]["vault.old"]["replaced_by"] == "vault.middle"
    assert registry.terminal_replacement("vault.old") == "vault.current"
    assert registry.predecessors("vault.current") == frozenset(
        {"vault.old", "vault.middle"}
    )


def test_replacement_cycles_are_refused() -> None:
    cycle = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "vault.one": _extension("One", status="deprecated", replaced_by="vault.two"),
                "vault.two": _extension("Two", status="deprecated", replaced_by="vault.one"),
            }
        )
    )
    assert "relation_cycle" in {finding["code"] for finding in cycle.findings}


def test_inactive_replacement_terminals_are_refused() -> None:
    inactive_terminal = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "vault.one": _extension("One", status="deprecated", replaced_by="vault.two"),
                "vault.two": _extension("Two", status="deprecated"),
            }
        )
    )
    assert "invalid_replacement" in {finding["code"] for finding in inactive_terminal.findings}


def test_inference_uses_origin_date_and_returns_incomplete_namespaced_candidates(
    tmp_path: Path,
) -> None:
    notes = tmp_path / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    for name, frontmatter in (
        ("created", "created: 2025-01-10"),
        ("captured", "captured: 2025-01-11"),
        ("fallback", "created: not-a-date\ncaptured: 2025-01-12"),
        ("excluded", "created: 2024-12-31"),
        ("undated", "updated: 2026-01-01"),
    ):
        (notes / f"{name}.md").write_text(
            f"---\ntype: insight\n{frontmatter}\n---\n# {name}\n\n## Relations\n"
            "- applies_to [[Knowledge Base/Notes/target]]\n",
            encoding="utf-8",
        )

    result = memory_schema.infer_relation_registry(
        tmp_path, date_from="2025-01-01", date_to="2025-01-31", recurrence_threshold=1
    )

    assert result["date_denominators"] == {
        "sampled": 5,
        "included": 3,
        "undated": 1,
        "excluded": 1,
    }
    assert len(result["promotion_candidates"]) == 1
    candidate = result["promotion_candidates"][0]
    assert {
        key: candidate[key]
        for key in (
            "canonical",
            "aliases",
            "parent",
            "description",
            "direction",
            "count",
        )
    } == {
        "canonical": "vault.applies_to",
        "aliases": ["applies_to"],
        "parent": None,
        "description": None,
        "direction": None,
        "count": 3,
    }
    assert [item["path"] for item in candidate["examples"]] == [
        "Knowledge Base/Notes/captured.md",
        "Knowledge Base/Notes/created.md",
        "Knowledge Base/Notes/fallback.md",
    ]
    assert candidate["raw_variants"] == [
        {
            "raw_relation": "applies_to",
            "count": 3,
            "examples": candidate["examples"],
        }
    ]
    assert candidate["raw_variants_total"] == 1
    assert candidate["raw_variants_truncated"] is False
    assert all(item["anchor"].startswith("line-") for item in candidate["examples"])


def test_inference_uses_a_noncolliding_namespaced_promotion_key(tmp_path: Path) -> None:
    relation_registry.save_registry(
        tmp_path,
        _proposal(
            **{
                "vault.science_replicates": _extension(
                    "A different reviewed meaning", aliases=["other_label"]
                )
            }
        ),
    )
    note = tmp_path / "Knowledge Base" / "Notes" / "source.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\ntype: insight\ncreated: 2025-01-10\n---\n# Source\n\n## Relations\n"
        "- science.replicates [[Knowledge Base/Notes/target]]\n",
        encoding="utf-8",
    )

    result = memory_schema.infer_relation_registry(tmp_path, recurrence_threshold=1)

    assert result["promotion_candidates"][0]["canonical"] == (
        "vault.science_replicates_2"
    )


@pytest.mark.parametrize(
    ("date_from", "date_to"),
    [("not-a-date", None), (None, "not-a-date"), ("2025-02-01", "2025-01-01")],
)
def test_inference_refuses_invalid_date_scopes(
    tmp_path: Path, date_from: str | None, date_to: str | None
) -> None:
    (tmp_path / "Knowledge Base").mkdir(parents=True)
    with pytest.raises(ValueError, match="INVALID_DATE_SCOPE"):
        memory_schema.infer_relation_registry(
            tmp_path, date_from=date_from, date_to=date_to
        )
