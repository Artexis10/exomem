"""A server's context-pack neighbourhood borrows the published inbound index.

The watcher owner is the only publisher that may build or rebuild the index in
a server process. A pack request that misses records demand and answers with
outbound neighbours, and the owner's rebuild or a write's patch restores
inbound ones. The explicit inbound command keeps its complete synchronous
lookup.
"""

from __future__ import annotations

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


def _pack_neighbours(vault: Path) -> tuple[list[str], bool]:
    page = find_module._CACHE.get(vault / ANCHOR, vault)
    assert page is not None
    neighbours, _dropped, inbound_complete = context_pack._neighborhood(vault, [page], 10)
    return sorted(item["path"] for item in neighbours), inbound_complete


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
    vault_module.on_inbound_files_changed(served, ["Knowledge Base/Notes/fresh.md"], [])

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
    vault_module.on_inbound_files_changed(served, [first], [])

    assert _pack_neighbours(served) == ([OUTLINKED], False)

    vault_module.on_inbound_files_changed(served, [second], [])

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
    vault_module.on_inbound_files_changed(served, [rewritten], [])  # reads the link
    _write_as_the_service_does(served, rewritten, "# Rewritten\n\nThe link is gone.\n")
    vault_module.on_inbound_files_changed(served, [pending], [])  # covers neither rewrite

    assert vault_module.cached_inbound_wikilinks(served, ANCHOR) is None

    vault_module.on_inbound_files_changed(served, [rewritten], [])

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
