"""D1-T2: cheap freshness accessors for the dreamer's gate and its carrier.

`generation` is one integer read under the registry lock: it must never derive
the triple, which rehashes the whole map after every change. `live_signature`
is one dict lookup: it must never copy the map the way `live_entries` does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import freshness


@pytest.fixture(autouse=True)
def _fresh_registry():
    freshness.clear()
    yield
    freshness.clear()


def _page(vault: Path, rel: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: insight\n---\n# Page\n", encoding="utf-8")
    return path


def _seed(vault: Path, *paths: Path) -> None:
    freshness.seed(vault, "vault", [(str(p), freshness.stat_signature(p)) for p in paths])


def test_generation_needs_no_triple(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "vault"
    first = _page(vault, "Knowledge Base/Notes/one.md")
    assert freshness.generation(vault, "vault") is None
    _seed(vault, first)
    seeded = freshness.generation(vault, "vault")
    assert isinstance(seeded, int)

    second = _page(vault, "Knowledge Base/Notes/two.md")
    freshness.on_files_changed(vault, changed=[second])

    def no_triple(*_args, **_kwargs):
        raise AssertionError("generation must not derive the triple")

    with monkeypatch.context() as patch:
        # The map just changed, so any triple read now would rehash it.
        patch.setattr(freshness, "triple_from_entries", no_triple)
        patch.setattr(freshness, "triple", no_triple)
        moved = freshness.generation(vault, "vault")
        assert moved is not None and moved > seeded
        assert freshness.generation(vault, "vault") == moved


def test_live_signature_is_a_point_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    page = _page(vault, "Knowledge Base/Notes/one.md")
    assert freshness.live_signature(vault, "vault", page) is None
    _seed(vault, page)

    def no_copy(*_args, **_kwargs):
        raise AssertionError("live_signature must not copy the live map")

    monkeypatch.setattr(freshness, "live_entries", no_copy)
    assert freshness.live_signature(vault, "vault", page) == freshness.stat_signature(page)
    assert freshness.live_signature(vault, "vault", str(page)) == freshness.stat_signature(page)
    assert freshness.live_signature(vault, "vault", vault / "Knowledge Base/absent.md") is None
    page.unlink()
    freshness.on_files_changed(vault, deleted=[page])
    assert freshness.live_signature(vault, "vault", page) is None


def test_live_signatures_batches_the_scope_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    one = _page(vault, "Knowledge Base/Notes/one.md")
    two = _page(vault, "Knowledge Base/Notes/two.md")
    absent = vault / "Knowledge Base/Notes/absent.md"
    assert freshness.live_signatures(vault, "vault", [one]) is None
    _seed(vault, one, two)
    keys: list[str] = []
    real_key = freshness._key
    monkeypatch.setattr(
        freshness, "_key", lambda root, scope: keys.append(scope) or real_key(root, scope)
    )
    assert freshness.live_signatures(vault, "vault", [one, str(two), absent]) == [
        freshness.stat_signature(one),
        freshness.stat_signature(two),
        None,
    ]
    assert keys == ["vault"]
