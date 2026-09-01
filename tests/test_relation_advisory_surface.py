from __future__ import annotations

import hashlib
import json

import pytest

from exomem import commands, mutation_terminal, writer_lease

_TARGET = (
    "Knowledge Base/Notes/Insights/"
    "progressive-disclosure-without-mode-fragmentation"
)


def _fact(label: str, status: str = "unregistered") -> dict[str, object]:
    return {
        "fact": {
            "raw_relation": label,
            "canonical_relation": None if status == "unregistered" else label,
            "registry_status": status,
        },
        "reasons": ["relation is unregistered"],
    }


def _leaf(container: str | None, facts: list[dict[str, object]]) -> dict[str, object]:
    semantic = {
        "contract_result": {
            "relation_disposition": {
                "kind": "missing",
                "rejected_facts": facts,
            }
        }
    }
    return semantic if container is None else {container: semantic}


def _compact(raw: dict[str, object]) -> dict[str, object]:
    return mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            raw,
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id="receipt",
            idempotency_key=None,
        ),
        "compact",
    )


@pytest.mark.parametrize("container", ["creation", "semantic", None])
def test_committed_unknown_relation_projects_one_central_bounded_advisory(
    container: str | None,
) -> None:
    compact = _compact(
        _leaf(container, [_fact("applies_to"), _fact("applies_to"), _fact("governs")])
    )

    assert compact["relation_advisory"] == {
        "raw_relations": ["applies_to", "governs"],
        "registry_hash": None,
        "recurrence_available": False,
        "occurrences": [],
        "truncated": False,
        "message": "Raw observations are preserved but untraversed until registered.",
        "next_action": {
            "tool": "connect_memory",
            "args": {"operation": "resolve-relation"},
        },
    }


@pytest.mark.parametrize(
    "facts",
    [
        [],
        [_fact("supports", "active")],
        [_fact("relates_to", "core")],
    ],
)
def test_non_unknown_relation_terminals_do_not_project_advisory(
    facts: list[dict[str, object]],
) -> None:
    assert "relation_advisory" not in _compact(_leaf("semantic", facts))


def test_advisory_labels_are_deduplicated_sorted_and_bounded() -> None:
    labels = [f"custom_{index:02d}" for index in reversed(range(20))]

    advisory = _compact(_leaf("semantic", [_fact(label) for label in labels]))[
        "relation_advisory"
    ]

    assert advisory["raw_relations"] == sorted(labels)[:8]
    assert advisory["truncated"] is True


def test_legacy_validation_and_failed_results_never_gain_an_advisory() -> None:
    raw = _leaf("semantic", [_fact("applies_to")])
    assert mutation_terminal.project_terminal(raw, "compact") is raw
    assert mutation_terminal.project_terminal(
        mutation_terminal.needs_review_terminal(
            {"mutated": False, "draft_id": "draft", "draft_hash": "hash", **raw}
        ),
        "compact",
    ).get("relation_advisory") is None


def test_advisory_projection_never_calls_corpus_or_embedding_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import exomem.epistemic_graph as graph
    import exomem.memory_schema as schema

    def forbidden(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("terminal advisory attempted unbounded work")

    monkeypatch.setattr(schema, "relation_observations", forbidden)
    monkeypatch.setattr(graph, "suggest_relations", forbidden)

    assert _compact(_leaf("semantic", [_fact("applies_to")]))["relation_advisory"]


def test_advisory_projects_bounded_indexed_recurrence_and_hides_lookup_context(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import exomem.memory_schema as schema

    calls: list[tuple[object, object]] = []

    def indexed(root, *, raw_relations, limit, offset):  # noqa: ANN001
        calls.append((root, raw_relations, limit, offset))
        return {
            "items": [
                {
                    "raw_relation": "applies_to",
                    "registry_status": "unregistered",
                    "count": 7,
                    "examples": [
                        {"path": "Knowledge Base/Notes/example.md", "anchor": "point"}
                    ],
                }
            ],
            "total": 1,
            "returned": 1,
            "omitted": 0,
            "offset": 0,
        }

    monkeypatch.setattr(schema, "indexed_relation_observations", indexed)
    raw = {
        **_leaf("semantic", [_fact("applies_to")]),
        "_relation_advisory_context": {
            "vault": str(tmp_path),
            "registry_hash": "a" * 64,
        },
    }
    terminal = mutation_terminal.committed_terminal(
        raw,
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id="receipt",
        idempotency_key=None,
    )

    compact = mutation_terminal.project_terminal(terminal, "compact")
    full = mutation_terminal.project_terminal(terminal, "full")
    legacy = mutation_terminal.project_terminal(terminal, "legacy")

    assert calls == [(tmp_path, ["applies_to"], 1, 0)] * 3
    assert compact["relation_advisory"] == full["relation_advisory"]
    assert compact["relation_advisory"]["recurrence_available"] is True
    assert compact["relation_advisory"]["occurrences"] == [
        {
            "raw_relation": "applies_to",
            "count": 7,
            "examples": [
                {"path": "Knowledge Base/Notes/example.md", "anchor": "point"}
            ],
        }
    ]
    assert "_relation_advisory_context" not in full["diagnostics"]
    assert "_relation_advisory_context" not in legacy


def test_advisory_normalizes_and_aggregates_indexed_spelling_variants(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import exomem.memory_schema as schema

    def indexed(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return {
            "items": [
                {
                    "raw_relation": "Applies-To",
                    "count": 3,
                    "examples": [
                        {"path": "a.md", "anchor": None},
                        {"path": "shared.md", "anchor": "same"},
                    ],
                },
                {
                    "raw_relation": "applies to",
                    "count": 4,
                    "examples": [
                        {"path": "b.md", "anchor": None},
                        {"path": "shared.md", "anchor": "same"},
                    ],
                },
                {
                    "raw_relation": "applies_to",
                    "count": 5,
                    "examples": [{"path": "c.md", "anchor": None}],
                },
            ],
            "total": 3,
            "returned": 3,
            "omitted": 0,
            "offset": 0,
        }

    monkeypatch.setattr(schema, "indexed_relation_observations", indexed)
    raw = {
        **_leaf("semantic", [_fact("Applies-To")]),
        "_relation_advisory_context": {"vault": str(tmp_path)},
    }

    advisory = _compact(raw)["relation_advisory"]

    assert advisory["raw_relations"] == ["applies_to"]
    assert advisory["recurrence_available"] is True
    assert advisory["occurrences"] == [
        {
            "raw_relation": "applies_to",
            "count": 12,
            "examples": [
                {"path": "a.md", "anchor": None},
                {"path": "shared.md", "anchor": "same"},
                {"path": "b.md", "anchor": None},
                {"path": "c.md", "anchor": None},
            ],
        }
    ]


@pytest.mark.parametrize("outcome", [None, RuntimeError("graph unavailable")])
def test_advisory_falls_back_when_current_graph_recurrence_is_unavailable(
    tmp_path, monkeypatch: pytest.MonkeyPatch, outcome: object
) -> None:
    import exomem.memory_schema as schema

    def indexed(*_args, **_kwargs):  # noqa: ANN002, ANN003
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(schema, "indexed_relation_observations", indexed)
    raw = {
        **_leaf("semantic", [_fact("applies_to")]),
        "_relation_advisory_context": {
            "vault": str(tmp_path),
            "registry_hash": "b" * 64,
        },
    }

    advisory = _compact(raw)["relation_advisory"]

    assert advisory["recurrence_available"] is False
    assert advisory["occurrences"] == []
    assert advisory["registry_hash"] == "b" * 64


def _invoke_public(
    vault,
    name: str,
    arguments: dict[str, object],
    *,
    detail: str = "compact",
) -> dict[str, object]:
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == name)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=vault.parent / f"{name}-writer-state")
    )
    return manager.invoke(
        command,
        (vault,),
        {**arguments, "response_detail": detail},
        idempotency_key=(
            f"relation-advisory-{name}-"
            + hashlib.sha256(
                json.dumps(arguments, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
        ),
    )


def _base_note(vault, slug: str) -> str:
    result = _invoke_public(
        vault,
        "remember",
        {
            "title": slug.replace("-", " ").title(),
            "slug": slug,
            "content": (
                f"# {slug.replace('-', ' ').title()}\n\n"
                "## Observations\n\n"
                "- [constraint] Keep relation review explicit. ^baseline\n\n"
                "## Relations\n"
                f"- relates_to [[{_TARGET}]]\n"
            ),
        },
    )
    assert "relation_advisory" not in result
    return str(result["path"])


def test_real_remember_public_terminal_projects_committed_unknown_relation(
    vault,
) -> None:
    result = _invoke_public(
        vault,
        "remember",
        {
            "title": "Public relation advisory",
            "slug": "public-relation-advisory",
            "content": (
                "# Public relation advisory\n\n"
                "## Observations\n\n"
                "- [constraint] Keep relation review explicit.\n\n"
                "## Relations\n"
                f"- relates_to [[{_TARGET}]]\n"
                f"- applies_to [[{_TARGET}]]\n"
            ),
        },
    )

    assert result["state"] == "committed"
    assert result["relation_advisory"]["raw_relations"] == ["applies_to"]  # type: ignore[index]


def test_advisory_can_read_point_recurrence_from_one_current_graph_snapshot(
    vault,
) -> None:
    from exomem import epistemic_graph, memory_schema

    _invoke_public(
        vault,
        "remember",
        {
            "title": "Indexed relation recurrence",
            "slug": "indexed-relation-recurrence",
            "content": (
                "# Indexed relation recurrence\n\n"
                "## Observations\n\n"
                "- [constraint] Keep recurrence evidence current.\n\n"
                "## Relations\n"
                f"- relates_to [[{_TARGET}]]\n"
                f"- applies_to [[{_TARGET}]]\n"
            ),
        },
    )
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()

    rows = memory_schema.indexed_relation_observations(
        vault, raw_relations=["applies_to"]
    )
    raw = {
        **_leaf("semantic", [_fact("applies_to")]),
        "_relation_advisory_context": {
            "vault": str(vault),
            "registry_hash": "c" * 64,
        },
    }
    advisory = _compact(raw)["relation_advisory"]

    assert rows is not None
    assert rows["items"][0]["raw_relation"] == "applies_to"
    assert rows["items"][0]["count"] >= 1
    assert advisory["recurrence_available"] is True
    assert advisory["occurrences"][0]["count"] >= 1


def test_real_graph_spelling_variants_form_one_advisory_occurrence(vault) -> None:
    from exomem import epistemic_graph, memory_schema

    committed = _invoke_public(
        vault,
        "remember",
        {
            "title": "Indexed relation spelling variants",
            "slug": "indexed-relation-spelling-variants",
            "content": (
                "# Indexed relation spelling variants\n\n"
                "## Observations\n\n"
                "- [constraint] Keep spelling evidence normalized.\n\n"
                "## Relations\n"
                f"- relates_to [[{_TARGET}]]\n"
                f"- applies_to [[{_TARGET}]]\n"
                f"- applies_to [[{_TARGET}-second]]\n"
                f"- applies_to [[{_TARGET}-third]]\n"
            ),
        },
    )
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    with index._connect() as connection:
        rows = connection.execute(
            "SELECT edge_key FROM graph_edges "
            "WHERE source_path = ? AND registry_status = 'unregistered' "
            "ORDER BY edge_key",
            (committed["path"],),
        ).fetchall()
        assert len(rows) == 3
        for (edge_key,), spelling in zip(
            rows, ("Applies-To", "applies to", "applies_to"), strict=True
        ):
            connection.execute(
                "UPDATE graph_edges SET raw_relation = ? WHERE edge_key = ?",
                (spelling, edge_key),
            )
        connection.commit()

    page = memory_schema.indexed_relation_observations(
        vault, raw_relations=["applies_to"], limit=8, offset=0
    )
    raw = {
        **_leaf("semantic", [_fact("Applies-To")]),
        "_relation_advisory_context": {"vault": str(vault)},
    }
    advisory = _compact(raw)["relation_advisory"]

    assert page is not None
    assert page["total"] == 1
    assert page["items"][0]["raw_relation"] == "applies_to"
    assert page["items"][0]["count"] == 3
    assert len(
        {
            (item["path"], item["anchor"])
            for item in page["items"][0]["examples"]
        }
    ) == len(page["items"][0]["examples"])
    assert advisory["recurrence_available"] is True
    assert advisory["occurrences"][0]["raw_relation"] == "applies_to"
    assert advisory["occurrences"][0]["count"] == 3


def test_real_observe_public_terminal_projects_committed_unknown_relation(vault) -> None:
    path = _base_note(vault, "observe-relation-advisory")

    result = _invoke_public(
        vault,
        "observe_memory",
        {
            "path": path,
            "operation": "add",
            "category": "decision",
            "kind": "decision",
            "content": "Keep the public terminal evidence-driven.",
            "id": "public-terminal",
            "relations": [
                {"kind": "applies_to", "target": _TARGET},
                {"kind": "relates_to", "target": _TARGET},
            ],
        },
    )

    assert result["state"] == "committed"
    assert result["relation_advisory"]["raw_relations"] == ["applies_to"]  # type: ignore[index]


def test_real_edit_public_terminal_projects_committed_unknown_relation(vault) -> None:
    path = _base_note(vault, "edit-relation-advisory")

    result = _invoke_public(
        vault,
        "edit_memory",
        {
            "path": path,
            "why": "add a reviewed raw relation observation",
            "operation": {
                "kind": "replace_string",
                "old_string": f"- relates_to [[{_TARGET}]]",
                "new_string": (
                    f"- relates_to [[{_TARGET}]]\n"
                    f"- applies_to [[{_TARGET}]]"
                ),
            },
        },
    )

    assert result["state"] == "committed"
    assert result["relation_advisory"]["raw_relations"] == ["applies_to"]  # type: ignore[index]


def test_real_replace_public_terminal_projects_committed_unknown_relation(vault) -> None:
    old_path = _base_note(vault, "replace-relation-advisory-old")

    result = _invoke_public(
        vault,
        "replace_memory",
        {
            "old_path": old_path,
            "title": "Replace relation advisory new",
            "slug": "replace-relation-advisory-new",
            "reason": "supersede with a more specific conclusion",
            "content": (
                "# Replace relation advisory new\n\n"
                "## Observations\n\n"
                "- [decision] Preserve an unknown authored label.\n\n"
                "## Relations\n"
                f"- relates_to [[{_TARGET}]]\n"
                f"- applies_to [[{_TARGET}]]\n"
            ),
        },
    )

    assert result["state"] == "committed"
    assert result["relation_advisory"]["raw_relations"] == ["applies_to"]  # type: ignore[index]


def test_real_validation_and_reviewed_no_edge_terminals_have_no_advisory(vault) -> None:
    arguments = {
        "title": "Reviewed no edge",
        "slug": "reviewed-no-edge",
        "content": (
            "# Reviewed no edge\n\n"
            "## Observations\n\n"
            "- [constraint] Do not invent a relation.\n"
        ),
    }
    validation_terminal = _invoke_public(
        vault, "remember", {**arguments, "validate_only": True}, detail="full"
    )
    assert validation_terminal["state"] == "needs_review"
    assert "relation_advisory" not in validation_terminal
    validation = validation_terminal["diagnostics"]

    committed = _invoke_public(
        vault,
        "remember",
        {
            **arguments,
            "draft_id": validation["draft_id"],
            "draft_hash": validation["draft_hash"],
            "draft_token": validation["draft_token"],
            "relation_disposition": "reviewed_none",
            "relation_review_hash": validation["relation_review_hash"],
            "relation_review_reason": "No honest typed relation applies.",
        },
    )
    assert committed["state"] == "committed"
    assert "relation_advisory" not in committed


def test_failed_public_write_never_commits_or_returns_relation_advisory(vault) -> None:
    destination = vault / "Knowledge Base/Notes/Insights/failed-unknown-only.md"

    with pytest.raises(ValueError, match="RELATION_DISPOSITION_MISSING") as failure:
        _invoke_public(
            vault,
            "remember",
            {
                "title": "Failed unknown only",
                "slug": "failed-unknown-only",
                "content": (
                    "# Failed unknown only\n\n"
                    "## Observations\n\n"
                    "- [constraint] Failed writes have no terminal advisory.\n\n"
                    "## Relations\n"
                    f"- applies_to [[{_TARGET}]]\n"
                ),
            },
        )

    assert "relation_advisory" not in str(failure.value)
    assert not destination.exists()
