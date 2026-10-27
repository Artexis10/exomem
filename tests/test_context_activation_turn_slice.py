"""The turn slice on the context-activation audit's fixtures, through `op_activate_context`.

The unpadded corpus tree is built and published through the supported writers, as the real-compiler
audit does. Every fixture turn is then compiled once as the shipped packet: the maps packet with the
turn slice on and the egress guard in place. The slice reuses the anchors compile, so it holds the
audit's own outcomes: the audit's passing positives lead with their gold, its passing negative twins
get no lead, and no guarded page is ever served as activated. The audit's red cases stay red here for
the reason `test_context_activation_real_compiler.py` records, and are not listed again.
"""

from __future__ import annotations

import pytest
from epistemic.corpora.context_activation import FixtureCase
from membench.utility import context_activation_product as product
from test_activate_context_turn_slice import full_compile_slices
from test_context_activation_real_compiler import PASSING_TODAY

pytestmark = [pytest.mark.timeout(1800)]

#: The leads that mean the turn activated a page; `partial`, `named` and `related` are served as unchecked.
ACTIVATING_MATCHES = ("resolved", "ambiguous", "carried")

_RUNS: dict[str, tuple[product.Tree, dict[str, dict]]] = {}
#: Each fixture's `turn_slice` from the full anchors compile, the one the shipped leads-only compile must equal.
_FULL: dict[str, dict] = {}


@pytest.fixture
def slices(tmp_path_factory: pytest.TempPathFactory) -> tuple[product.Tree, dict[str, dict]]:
    """Each unpadded-tree fixture's `turn_slice`, compiled once for the module as a cold maps activation."""
    if "run" not in _RUNS:
        tree = product.prepare_tree(
            tmp_path_factory.mktemp("turn-slice-fixtures") / "tree-0", distractor_count=0
        )
        _RUNS["run"] = (
            tree,
            {fixture.case_id: product.activate(tree.root, fixture)["turn_slice"] for fixture in tree.fixtures},
        )
        with pytest.MonkeyPatch.context() as scope:
            full_compile_slices(scope)
            _FULL.update(
                {fixture.case_id: product.activate(tree.root, fixture)["turn_slice"] for fixture in tree.fixtures}
            )
    return _RUNS["run"]


def _leads(block: dict) -> dict[str, dict]:
    """The items by ref and by page path: the audit binds a collection to its page, the slice names it by record."""
    return {name: item for item in block["items"] for name in (item["ref"], item.get("path")) if name}


def _refs(tree: product.Tree, keys: tuple[str, ...]) -> set[str]:
    bound = dict(tree.binding.key_to_ref)
    return {bound[key] for key in keys}


def _fixtures(tree: product.Tree, *, passing: bool, resolving: bool) -> list[FixtureCase]:
    return [
        fixture
        for fixture in tree.fixtures
        if (fixture.case_id in PASSING_TODAY) == passing
        and (fixture.expected_status in ("resolved", "ambiguous")) == resolving
    ]


def test_a_passing_positive_fixture_leads_with_its_gold(slices) -> None:
    tree, blocks = slices
    positives = _fixtures(tree, passing=True, resolving=True)
    assert positives, "the audit passes no positive fixture on this tree"
    for fixture in positives:
        block = blocks[fixture.case_id]
        assert block["state"] == "ready", (fixture.case_id, block)
        missing = _refs(tree, fixture.gold) - set(_leads(block))
        assert not missing, (fixture.case_id, sorted(missing), block)


def test_a_passing_negative_twin_gets_no_lead(slices) -> None:
    tree, blocks = slices
    twins = _fixtures(tree, passing=True, resolving=False)
    assert twins, "the audit passes no negative fixture on this tree"
    for fixture in twins:
        assert blocks[fixture.case_id]["state"] == "empty", (fixture.case_id, blocks[fixture.case_id])
        assert blocks[fixture.case_id]["items"] == [], fixture.case_id


def test_a_guarded_page_is_never_served_as_an_activated_lead(slices) -> None:
    """A poison page may still be listed as an unchecked lead (T6's partial grill, red in the audit too)."""
    tree, blocks = slices
    for fixture in tree.fixtures:
        leads = _leads(blocks[fixture.case_id])
        for ref in _refs(tree, fixture.poison) & set(leads):
            assert leads[ref]["match"] not in ACTIVATING_MATCHES, (fixture.case_id, leads[ref])


def test_every_fixture_gets_the_full_compiles_slice_from_the_leads_only_compile(slices) -> None:
    """The slice skips the lanes whose material it never serves: each fixture's leads, their match and
    evidence, and the slice's state are the ones the full anchors compile gives."""
    tree, blocks = slices
    assert set(_FULL) == {fixture.case_id for fixture in tree.fixtures}
    for case_id, block in blocks.items():
        assert block == _FULL[case_id], case_id
