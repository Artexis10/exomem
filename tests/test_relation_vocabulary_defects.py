"""Two filed relation-vocabulary defects, each reproduced before it is fixed.

Both were found while building the structural relation suggesters and recorded
rather than fixed there, because neither belonged to that change.
"""

from __future__ import annotations

import pytest

from exomem import epistemic_graph, relation_queue, relation_registry


def _proposal(**extensions):
    return {"schema_version": 1, "extensions": extensions}


def _with_alias(alias: str):
    return relation_registry.load_registry(
        proposal=_proposal(
            **{
                "science.replicates": {
                    "parent": "supports",
                    "description": "Reports independent reproduction",
                    "aliases": [alias],
                }
            }
        )
    )


# --------------------------------------------------------------------------
# An alias that fails the label grammar must not be registered
# --------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["has!bang", "ünicode", "trailing/slash"])
def test_an_invalid_alias_is_recorded_and_refused(alias: str) -> None:
    """The finding was recorded and the alias registered anyway.

    `_parse_extension_data` appends an `invalid_alias` finding and then falls
    through to the collision check, whose `else` branch registers the alias — so
    a label the grammar rejects still resolved with `alias` standing, exactly as
    though it had passed validation. The spec requires an extension to "pass
    alias, inverse, node-kind, origin, status, and scope validation", so
    registering it contradicts a live requirement rather than merely looking
    untidy.
    """
    registry = _with_alias(alias)

    assert "invalid_alias" in {finding["code"] for finding in registry.findings}
    assert registry.resolve(alias).status != "alias"
    assert alias not in registry.aliases


def test_normalization_still_rescues_a_merely_untidy_alias() -> None:
    """The control, and the reason this is narrower than it first looks.

    `Not Valid` and `Replicates ` are not invalid — `normalize_relation`
    lowercases and underscores them into `not_valid` and `replicates`, which the
    grammar accepts. Refusing invalid aliases must not start refusing these.
    """
    for authored, normalized in (("Not Valid", "not_valid"), ("Replicates ", "replicates")):
        registry = _with_alias(authored)
        assert not [f for f in registry.findings if f["code"] == "invalid_alias"], authored
        assert registry.resolve(normalized).status == "alias"


def test_a_valid_alias_still_registers() -> None:
    registry = _with_alias("replicates")

    assert registry.resolve("replicates").status == "alias"
    assert registry.aliases["replicates"] == "science.replicates"


# --------------------------------------------------------------------------
# Candidates are classified before the display budget is spent
# --------------------------------------------------------------------------


def _write_page(vault, name: str, body: str) -> None:
    path = vault / "Knowledge Base" / "Notes" / "Insights" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: insight\nstatus: active\n---\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )


def _crowded_vault(vault, *, authored: int, fresh: int) -> None:
    """One page whose first candidates are all already-authored edges."""
    targets = [f"t{index:02d}" for index in range(authored + fresh)]
    for target in targets:
        _write_page(vault, target, "A measured fact.")

    # `## Relations` first, so the authored edges are also the earliest
    # wikilinks in the body and therefore the earliest candidates.
    relations = "\n".join(
        f"- links_to [[Knowledge Base/Notes/Insights/{target}]]"
        for target in targets[:authored]
    )
    mentions = " ".join(
        f"[[Knowledge Base/Notes/Insights/{target}]]" for target in targets
    )
    _write_page(vault, "crowded", f"## Relations\n\n{relations}\n\nAlso see {mentions}.")
    # Queue reads require a published graph; fixture writes are deliberately raw.
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()


def test_the_display_budget_is_not_spent_on_filtered_candidates(vault) -> None:
    """Truncation ran before classification, so drops came out of the budget.

    `suggest_relations` capped at `limit_per_page` and only then did
    `relation_queue` drop the already-authored and already-decided ones. A page
    whose first ten candidates are all authored therefore produced an empty
    group — and the genuinely open candidates behind them never surfaced at all,
    no matter how many times the queue was read.
    """
    limit = 10
    _crowded_vault(vault, authored=limit, fresh=3)

    queue = relation_queue.build_queue(vault, limit_per_page=limit)
    assert queue["status"] == "available"
    crowded = [
        group for group in queue["groups"] if group["path"].endswith("crowded.md")
    ]

    assert crowded, "the page with open candidates produced no group at all"
    assert {item["to"] for item in crowded[0]["items"]} == {
        f"Knowledge Base/Notes/Insights/t{index:02d}.md"
        for index in range(limit, limit + 3)
    }, "filtering must retain every open candidate behind the authored prefix"


def test_the_budget_still_bounds_what_is_shown(vault) -> None:
    """The control: over-fetching to classify must not widen the display cap."""
    limit = 5
    _crowded_vault(vault, authored=0, fresh=12)

    queue = relation_queue.build_queue(vault, limit_per_page=limit)
    assert queue["status"] == "available"
    crowded = [
        group for group in queue["groups"] if group["path"].endswith("crowded.md")
    ]

    assert crowded
    assert len(crowded[0]["items"]) == limit
