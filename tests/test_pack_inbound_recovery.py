"""A server's context-pack neighbourhood borrows the published inbound index.

The watcher owner is the only publisher that may build or rebuild the index in
a server process. A pack request that misses records demand and answers with
outbound neighbours, and the owner's rebuild or a write's patch restores
inbound ones. The explicit inbound command keeps its complete synchronous
lookup.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from exomem import context_pack, freshness, lexstore
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.find_types import Hit
from exomem.list_inbound_links import list_inbound_links

ANCHOR = "Knowledge Base/Notes/anchor.md"
OUTLINKED = "Knowledge Base/Notes/outlinked.md"
BACKLINKER = "Knowledge Base/Notes/backlinker.md"


def _write(vault: Path, rel: str, body: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _write_as_the_service_does(vault: Path, rel: str, body: str) -> None:
    """A committed service write: the file lands, then freshness announces it."""
    freshness.on_files_changed(vault, changed=[_write(vault, rel, body)])


def _fan_out(vault: Path, changed: list[str]) -> None:
    """The watcher's fan-out of one batch: the link resolver, then the inbound index."""
    find_module.on_resolver_files_changed(vault, changed, [])
    vault_module.on_inbound_files_changed(vault, changed, [])


def _pack_gaps(vault: Path) -> tuple[list[str], list[str]]:
    page = find_module._CACHE.get(vault / ANCHOR, vault)
    assert page is not None
    neighbours, _dropped, incomplete = context_pack._neighborhood(vault, [page], 10)
    return sorted(item["path"] for item in neighbours), incomplete


def _pack_neighbours(vault: Path) -> tuple[list[str], bool]:
    """Neighbour paths, and whether the inbound side was complete."""
    paths, incomplete = _pack_gaps(vault)
    return paths, context_pack.INBOUND_INCOMPLETE_TRUNCATION not in incomplete


@pytest.fixture
def served(tmp_path: Path) -> Path:
    """A vault in a server process: freshness is live and the watcher owner is registered."""
    vault = tmp_path / "vault"
    _write(vault, ANCHOR, "# Anchor\n\nAnchor lede. See [[Notes/outlinked]].\n")
    _write(vault, OUTLINKED, "# Outlinked\n\nOutlinked lede.\n")
    _write(vault, BACKLINKER, "# Backlinker\n\nBacklinker lede. See [[Notes/anchor]].\n")
    find_module.clear_cache()
    vault_module.clear_inbound_index()
    entries = [(str(p), freshness.stat_signature(p)) for p in vault.rglob("*.md")]
    freshness.seed(vault, "kb", entries)
    freshness.seed(vault, "vault", entries)
    lexstore.ensure_fresh(vault)
    vault_module.register_inbound_index(vault)
    find_module._get_query_resolver(vault)  # a warm server holds the link resolver
    return vault


def _forbid_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_root: Path):
        raise AssertionError("a request thread built the inbound index")

    monkeypatch.setattr(vault_module, "_build_inbound_index", refuse)


def test_cold_pack_returns_outbound_neighbours_and_never_builds(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request that finds no index must not pay the vault-sized build."""
    _forbid_builds(monkeypatch)
    demanded = vault_module._INBOUND_INDEX_GENERATIONS[str(served.resolve())].demanded
    assert not demanded.is_set()

    pack = context_pack.assemble_pack(
        served, [Hit(path=ANCHOR, type=None, scope=None, title="", updated="", excerpt="")]
    )

    assert [item["path"] for item in pack["neighborhood"]] == [OUTLINKED]
    assert context_pack.INBOUND_INCOMPLETE_TRUNCATION in pack["truncation"]
    assert demanded.is_set(), "the miss left the watcher owner no signal to rebuild"


def test_the_owner_rebuild_restores_inbound_neighbours_for_the_next_pack(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _pack_neighbours(served) == ([OUTLINKED], False)

    assert vault_module.prime_inbound_index(served, eager=True)

    _forbid_builds(monkeypatch)
    assert _pack_neighbours(served) == ([BACKLINKER, OUTLINKED], True)


def test_a_self_written_link_reaches_the_next_pack_without_a_rebuild(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert vault_module.prime_inbound_index(served, eager=True)
    _forbid_builds(monkeypatch)

    _write_as_the_service_does(served, "Knowledge Base/Notes/fresh.md", "# Fresh\n\n[[Notes/anchor]]\n")
    _fan_out(served, ["Knowledge Base/Notes/fresh.md"])

    assert _pack_neighbours(served) == (
        sorted([BACKLINKER, OUTLINKED, "Knowledge Base/Notes/fresh.md"]),
        True,
    )


def test_overlapping_self_writes_keep_the_index_published_once_both_are_patched(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constant writes: a second write lands before the first one's patch runs.

    The first patch cannot yet vouch for the second file, so the pack declines
    the inbound side (never a stale answer). The second patch completes the
    history, and the same index serves both links with no rebuild.
    """
    assert vault_module.prime_inbound_index(served, eager=True)
    _forbid_builds(monkeypatch)
    first, second = "Knowledge Base/Notes/first.md", "Knowledge Base/Notes/second.md"

    _write_as_the_service_does(served, first, "# First\n\n[[Notes/anchor]]\n")
    _write_as_the_service_does(served, second, "# Second\n\n[[Notes/anchor]]\n")
    _fan_out(served, [first])

    assert _pack_neighbours(served) == ([OUTLINKED], False)

    _fan_out(served, [second])

    assert _pack_neighbours(served) == (sorted([first, second, BACKLINKER, OUTLINKED]), True)


def test_a_patch_cannot_vouch_for_a_file_that_changed_again_after_it_was_read(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale copy of a page must never reach a pack as complete evidence."""
    assert vault_module.prime_inbound_index(served, eager=True)
    _forbid_builds(monkeypatch)
    pending, rewritten = "Knowledge Base/Notes/pending.md", "Knowledge Base/Notes/rewritten.md"

    _write_as_the_service_does(served, pending, "# Pending\n")
    _write_as_the_service_does(served, rewritten, "# Rewritten\n\n[[Notes/anchor]]\n")
    _fan_out(served, [rewritten])  # reads the link
    _write_as_the_service_does(served, rewritten, "# Rewritten\n\nThe link is gone.\n")
    _fan_out(served, [pending])  # covers neither rewrite

    assert vault_module.cached_inbound_wikilinks(served, ANCHOR) is None

    _fan_out(served, [rewritten])

    assert _pack_neighbours(served) == ([BACKLINKER, OUTLINKED], True)


def test_a_patch_that_history_cannot_bridge_withdraws_the_index_for_the_owner(
    served: Path,
) -> None:
    """Data stamped current without proof would serve a stale pack. Only the owner rebuilds it."""
    assert vault_module.prime_inbound_index(served, eager=True)
    freshness.seed(  # a reconcile that replaced the registry: no history bridges the old stamp
        served, "vault", [(str(p), freshness.stat_signature(p)) for p in served.rglob("*.md")]
    )
    _write_as_the_service_does(served, BACKLINKER, "# Backlinker\n\nThe link is gone.\n")
    vault_module.on_inbound_files_changed(served, [BACKLINKER], [])
    find_module._get_query_resolver(served)  # the resolver is rebuilt in the background too

    assert _pack_neighbours(served) == ([OUTLINKED], False)
    assert vault_module.prime_inbound_index(served, eager=True)
    assert _pack_neighbours(served) == ([OUTLINKED], True)


def test_explicit_inbound_listing_still_builds_synchronously_when_no_index_is_live(
    served: Path,
) -> None:
    """`list_inbound_links` is a complete answer, so it may pay for the build."""
    assert str(served.resolve()) not in vault_module._INBOUND_INDEX

    listing = list_inbound_links(served, target=ANCHOR)

    assert [item["path"] for item in listing.inbound] == [BACKLINKER]


def test_a_pack_miss_starts_one_background_build_that_publishes_without_a_reconcile_pass(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconcile pass is minutes away: a demanded miss must not wait for it.
    A second miss, or that pass itself, must not start a second build of the vault."""
    monkeypatch.delenv("EXOMEM_DISABLE_RESOLVER_WARM")
    real_build = vault_module._build_inbound_index
    started, release = threading.Event(), threading.Event()
    builders: list[str] = []

    def paused_build(root: Path):
        builders.append(threading.current_thread().name)
        started.set()
        assert release.wait(20)
        return real_build(root)

    monkeypatch.setattr(vault_module, "_build_inbound_index", paused_build)

    assert _pack_neighbours(served) == ([OUTLINKED], False)
    assert started.wait(10), "the miss started no background build"
    assert _pack_neighbours(served) == ([OUTLINKED], False)  # a second miss, mid-build
    assert not vault_module.prime_inbound_index(served, eager=True)  # the reconcile pass defers

    release.set()
    assert find_module.await_recall_resolver_warm()

    assert builders == ["exomem-inbound-index-warm"]
    assert _pack_neighbours(served) == ([BACKLINKER, OUTLINKED], True)


def test_a_pack_that_misses_only_while_a_patch_is_pending_does_not_start_a_rebuild(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each write leaves a short window before its patch runs. A whole-vault rebuild per
    pack in that window would cost more than the patch that closes it."""
    assert vault_module.prime_inbound_index(served, eager=True)
    monkeypatch.delenv("EXOMEM_DISABLE_RESOLVER_WARM")
    real_build = vault_module._build_inbound_index
    builds: list[int] = []

    def counted_build(root: Path):
        builds.append(1)
        return real_build(root)

    monkeypatch.setattr(vault_module, "_build_inbound_index", counted_build)
    fresh = "Knowledge Base/Notes/fresh.md"
    _write_as_the_service_does(served, fresh, "# Fresh\n\n[[Notes/anchor]]\n")
    find_module.on_resolver_files_changed(served, [fresh], [])  # the inbound patch is still to run

    assert _pack_neighbours(served) == ([OUTLINKED], False)
    assert find_module.await_recall_resolver_warm()

    assert builds == []
    vault_module.on_inbound_files_changed(served, [fresh], [])
    assert _pack_neighbours(served) == (sorted([BACKLINKER, OUTLINKED, fresh]), True)


def _forbid_resolver_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("a request thread built the link resolver")

    monkeypatch.setattr(vault_module, "WikilinkResolver", refuse)


def test_cold_resolver_pack_is_marked_incomplete_and_never_builds_it(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request that finds no link resolver must not pay the vault-sized build."""
    find_module.evict_resolver_caches(served)
    _forbid_resolver_builds(monkeypatch)

    pack = context_pack.assemble_pack(
        served, [Hit(path=ANCHOR, type=None, scope=None, title="", updated="", excerpt="")]
    )

    assert pack["neighborhood"] == []
    assert context_pack.OUTBOUND_INCOMPLETE_TRUNCATION in pack["truncation"]


def test_the_background_owner_rebuilds_the_resolver_a_pack_found_missing(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_RESOLVER_WARM")
    find_module.evict_resolver_caches(served)
    paths, incomplete = _pack_gaps(served)
    assert paths == []
    assert context_pack.OUTBOUND_INCOMPLETE_TRUNCATION in incomplete

    assert find_module.await_recall_resolver_warm()

    _forbid_resolver_builds(monkeypatch)
    # The same miss also restored the inbound index, so both sides are complete.
    assert _pack_gaps(served) == ([BACKLINKER, OUTLINKED], [])


def test_a_resolver_the_watcher_has_not_yet_patched_is_declined_not_served_stale(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link added by a write the resolver has not absorbed must not be resolved
    against the old maps: the pack reports the gap until the patch lands."""
    new = "Knowledge Base/Notes/later.md"
    _write_as_the_service_does(served, ANCHOR, "# Anchor\n\nSee [[Notes/outlinked]] and [[Notes/later]].\n")
    _write_as_the_service_does(served, new, "# Later\n")
    _forbid_resolver_builds(monkeypatch)

    assert _pack_gaps(served)[0] == []
    assert context_pack.OUTBOUND_INCOMPLETE_TRUNCATION in _pack_gaps(served)[1]

    find_module.on_resolver_files_changed(served, [ANCHOR, new], [])

    assert _pack_gaps(served)[0] == [new, OUTLINKED]


def _self_write(vault: Path, name: str) -> str:
    """A service write whose link lands on the anchor, fanned out to the inbound patch."""
    rel = f"Knowledge Base/Notes/{name}.md"
    _write_as_the_service_does(vault, rel, f"# {name}\n\n[[Notes/anchor]]\n")
    _fan_out(vault, [rel])
    return rel


def test_writes_that_land_during_the_owner_build_do_not_discard_it(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under steady writes a build that every patch discarded would never publish,
    and now that requests never build, packs would stay incomplete for good."""
    real_build = vault_module._build_inbound_index
    builds: list[int] = []
    during: list[str] = []

    def build_while_writes_land(root: Path):
        builds.append(1)
        data = real_build(root)
        during.extend(_self_write(served, f"during-{n}") for n in range(3))
        return data

    monkeypatch.setattr(vault_module, "_build_inbound_index", build_while_writes_land)

    assert vault_module.prime_inbound_index(served, eager=True)

    assert len(builds) == 1
    assert _pack_neighbours(served) == (sorted([*during, BACKLINKER, OUTLINKED]), True)


def test_a_write_stream_that_outlasts_one_round_still_publishes_within_the_round_bound(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_plan = vault_module._plan_inbound_edit
    rounds: list[int] = []
    streamed: list[str] = []

    def plan_then_write(root, changed, deleted):
        edit = real_plan(root, changed, deleted)
        rounds.append(1)
        if len(rounds) <= 3:  # the stream keeps writing while the catch-up replays it
            streamed.append(_self_write(served, f"stream-{len(rounds)}"))
        return edit

    monkeypatch.setattr(vault_module, "_plan_inbound_edit", plan_then_write)

    assert vault_module.prime_inbound_index(served, eager=True)

    assert len(rounds) <= vault_module._INBOUND_CATCH_UP_ROUNDS
    assert _pack_neighbours(served) == (sorted([*streamed, BACKLINKER, OUTLINKED]), True)


def test_a_write_stream_that_never_pauses_stops_the_catch_up_and_keeps_demand(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_plan = vault_module._plan_inbound_edit
    rounds: list[int] = []
    stream_open = True

    def plan_then_write(root, changed, deleted):
        edit = real_plan(root, changed, deleted)
        rounds.append(1)
        if stream_open:
            _self_write(served, f"endless-{len(rounds)}")
        return edit

    monkeypatch.setattr(vault_module, "_plan_inbound_edit", plan_then_write)
    demanded = vault_module._INBOUND_INDEX_GENERATIONS[str(served.resolve())].demanded

    assert not vault_module.prime_inbound_index(served, eager=True)

    assert len(rounds) == vault_module._INBOUND_CATCH_UP_ROUNDS
    assert demanded.is_set()
    assert vault_module.cached_inbound_wikilinks(served, ANCHOR) is None

    stream_open = False  # the writes pause: the owner's next pass publishes
    assert vault_module.prime_inbound_index(served)
    assert _pack_neighbours(served)[1] is True
