"""The read path must not enumerate the corpus, and the sentinel must be able to see it.

`filter_eligibility` (18.1 s) and `outside_kb` (7.6 s) on the live cell on
2026-09-02 were two whole-vault walks per request. A latency threshold would
have caught them late and noisily; a structural sentinel catches them on any
box, because a stage either enumerated the scope or it did not.

The contract governs the MANAGED read path: an offline/CLI caller keeps its
exact source-walk fallback by design, so every assertion here is taken against
a warm managed cell — the registry seeded, the catalogue published, admission
ready — which is the configuration the live cell runs in.

Four shapes, all of them now pinned:

* the unfiltered `scope="kb-only"` hybrid recall, which already answers from
  the maintained indexes — a *pin*, so a later change that reintroduces a walk
  under it turns this red;
* the same recall carrying a supported page filter, which Lane 2 moved onto
  the page catalogue. It was `xfail(strict=True)` while the plan fell through
  to the canonical full-scan oracle; it is a plain assertion now, so a change
  that puts the filtered read path back on the walk turns this red;
* the sentinel's own non-vacuity, proven by driving the scan oracle directly.
  A sentinel that cannot count a walk it is pointed at proves nothing about
  the two above;
* the cold reference sidecar, which used to rebuild inline from a corpus scan.
  Lane 3's read-side exact custody made a managed reader decline there instead,
  so it is a plain assertion now and a change that puts the rebuild back on the
  request thread turns this red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, structured_filters
from exomem import find as find_module
from exomem.vault import kb_dirname


def _scope_roots(vault: Path) -> tuple[Path, ...]:
    """Every directory the contract forbids enumerating on the reader thread.

    The vault root subsumes the knowledge-base scope, so one root would do for
    a KB walk — but `walk_vault_md` and the out-of-KB widening reach sibling
    trees (`Reference/`, `Tracking/`) that a KB-rooted counter would miss.
    Both are named so the counter covers the widening lane too.
    """
    return (vault, vault / kb_dirname())


def _projects_plan() -> structured_filters.FilterPlan:
    return structured_filters.compile_filter(
        None,
        shortcuts=structured_filters.FilterShortcuts(projects=("project-alpha",)),
    )


def test_unfiltered_kb_only_hybrid_recall_enumerates_no_pages(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """PIN: the warm managed read path already answers without walking the scope."""
    warm_managed_cell(vault)
    sentinel = walk_sentinel(*_scope_roots(vault))

    sentinel.reset()
    commands.op_find(
        vault,
        query="metabolism",
        mode="hybrid",
        scope="kb-only",
        graph=False,
        include_timings=True,
    )

    assert sentinel.count == 0, sentinel.report()


def test_filtered_hybrid_recall_enumerates_no_pages(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """Lane 2's contract: a supported page filter resolves from an index."""
    warm_managed_cell(vault)
    sentinel = walk_sentinel(*_scope_roots(vault))

    sentinel.reset()
    commands.op_find(
        vault,
        query="metabolism",
        projects=["project-alpha"],
        mode="hybrid",
        scope="kb-only",
        graph=False,
        include_timings=True,
    )

    assert sentinel.count == 0, sentinel.report()


def test_sentinel_counts_a_real_walk(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """The sentinel is not vacuous: the scan oracle it must catch registers.

    `_eligible_filter_paths` IS the walk the contract forbids on the reader
    thread. Driving it directly proves the counter sees the enumeration
    primitive the oracle actually uses, so a zero above is silence, not
    blindness.
    """
    warm_managed_cell(vault)
    sentinel = walk_sentinel(*_scope_roots(vault))

    sentinel.reset()
    eligible = find_module._eligible_filter_paths(
        vault, scope="kb", plan=_projects_plan()
    )

    assert eligible, "the oracle must resolve some pages, or it walked for nothing"
    assert sentinel.count > 0, "the sentinel did not see the canonical scan oracle walk"


def test_cold_refs_sidecar_declines_instead_of_walking(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """The spec's other decline case, unpinned until now.

    "An unanswerable filter declines instead of walking" is stated for
    eligibility, but the rule is about the reader thread, not about one stage.
    The reference sidecar is a maintained index like any other, and when it was
    not current `refs_for_paths` rebuilt it inline from a full corpus scan —
    40 scope enumerations inside `serialize`, on the request. A managed reader
    owes the typed warming outcome there for the same reason it owes it for a
    filter it cannot answer from an index, and one background rebuild pays for
    the scan off the request.
    """
    warm_managed_cell(vault, prebuild_refs=False)
    sentinel = walk_sentinel(*_scope_roots(vault))

    sentinel.reset()
    with pytest.raises(find_module.RetrievalIndexWarming):
        commands.op_find(
            vault,
            query="metabolism",
            mode="hybrid",
            scope="kb-only",
            graph=False,
            include_timings=True,
        )

    assert sentinel.count == 0, sentinel.report()
