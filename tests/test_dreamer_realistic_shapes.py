"""The dreamer on the shapes real vaults have (review round 2).

The first fixture had no `## Relations` section, no `exomem_id` and no Source
that many notes cite. Real vaults have all three, and each broke a rule:
authored relations built a whole-vault resolver, id-bearing pages wedged the
pass on a cold identity cache, a one-Source cluster filled the family cap for
good, and one Source edit fanned out into unbounded work inside one page.
"""

from __future__ import annotations

import time
from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import (
    commands,
    dreamer,
    dreamer_families,
    dreamer_store,
    freshness,
    semantic_contract,
    upkeep,
)
from exomem import vault as vault_module

LATER = time.time() + 3 * 3600


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    semantic_contract.reset_corpus_context_cache()
    upkeep.reset_delivery_state()
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()
    semantic_contract.reset_corpus_context_cache()
    upkeep.reset_delivery_state()


def _quiet(vault: Path, *, now: float | None = None, limit: int = 60) -> list:
    results = fx.run_to_quiet(vault, now=now, limit=limit)
    assert all(result.stop_reason != "error" for result in results), results
    return results


def _rows(vault: Path, family: str | None = None, state: str | None = "open") -> list[dict]:
    view = dreamer_store.read_view(vault)
    return [
        row
        for row in (view.candidates if view else ())
        if (family is None or row["family"] == family) and (state is None or row["state"] == state)
    ]


class _Builds:
    """Counts whole-vault resolver builds and vault walks."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.builds = 0
        self.walks = 0
        real_build = vault_module.WikilinkResolver._build
        real_walk = vault_module.walk_vault_md

        def build(resolver):
            self.builds += 1
            return real_build(resolver)

        def walk(*args, **kwargs):
            self.walks += 1
            return real_walk(*args, **kwargs)

        monkeypatch.setattr(vault_module.WikilinkResolver, "_build", build)
        monkeypatch.setattr(vault_module, "walk_vault_md", walk)


# ----------------------------------------------------------------------
# F6: authored relations never build a whole-vault resolver
# ----------------------------------------------------------------------


def test_a_tick_over_relations_builds_no_resolver_and_walks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = fx.build_realistic(tmp_path)
    fx.warm_identity(vault, monkeypatch)
    spy = _Builds(monkeypatch)
    _quiet(vault)
    assert spy.builds == 0 and spy.walks == 0, (spy.builds, spy.walks)
    # The pass still proposes: the unauthored pair, and hydration.
    families = {row["family"] for row in _rows(vault)}
    assert families == {dreamer_families.LINK_FAMILY, dreamer_families.HYDRATION_FAMILY}


def test_authored_relations_still_suppress_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = fx.build_realistic(tmp_path, with_graph=False)
    # The inlet note now authors a relation to the cavitation note.
    text = (vault / fx.INLET).read_text(encoding="utf-8")
    fx.write(vault, fx.INLET, text + "- relates_to [[Notes/Insights/pump-cavitation]]\n")
    freshness.clear()
    fx.seed(vault)
    fx.publish_graph(vault)
    fx.warm_identity(vault, monkeypatch)
    spy = _Builds(monkeypatch)
    _quiet(vault)
    assert spy.builds == 0
    subjects = {row["subject_path"] for row in _rows(vault, dreamer_families.LINK_FAMILY)}
    assert subjects == set()


def test_item_and_context_over_relations_build_no_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = fx.build_realistic(tmp_path)
    fx.warm_identity(vault, monkeypatch)
    _quiet(vault)
    link = _rows(vault, dreamer_families.LINK_FAMILY)[0]
    ref = upkeep.upkeep_ref(link["id"])
    spy = _Builds(monkeypatch)
    commands.op_review_memory(vault, mode="item", ref=ref)
    commands.op_review_item_context(vault, ref=ref)
    assert spy.builds == 0 and spy.walks == 0, (spy.builds, spy.walks)
