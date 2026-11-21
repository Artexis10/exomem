"""Top-index writers use stored pages while their projections lag or disappear."""

from __future__ import annotations

import datetime as dt

import pytest
from test_vault_store_write_path import coordinator_url, served, stored, vault, writer  # noqa: F401

from exomem import adopt, preserve
from exomem import vault as vault_module
from exomem.vault_store import read, write

pytestmark = pytest.mark.timeout(180)


@pytest.mark.parametrize("projection", ["stale", "deleted"])
def test_adoption_manifest_keeps_unprojected_recent_activity(served, writer, projection):  # noqa: F811
    """Saving a manifest must not overwrite activity that only the store holds."""
    rel = "Knowledge Base/index.md"
    path = served / rel
    earlier = "- 2026-01-01: Captured the harbour survey.\n"
    with read.snapshot(served) as view:
        current = view.page(rel).body.decode("utf-8")
    updated = current.replace("## Recent activity\n", "## Recent activity\n\n" + earlier)
    assert earlier in updated
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "test", puts=(write.PagePut(rel, updated.encode("utf-8")),),
    ))
    assert earlier not in path.read_text(encoding="utf-8")
    if projection == "deleted":
        path.unlink()

    result = adopt.adopt(served, mode="save-manifest", today=dt.date(2026, 1, 2))

    with read.snapshot(served) as view:
        actual = view.page(rel).body.decode("utf-8")
        assert view.page(result["manifest"]["path"]) is not None
    assert earlier in actual
    assert "adoption manifest" in actual
    assert not any("index.md missing" in warning for warning in result["manifest"]["warnings"])


def test_preserve_updates_a_stored_top_index_without_a_projection(served):  # noqa: F811
    """Preserving evidence must not skip recent activity because its index file is absent."""
    rel = "Knowledge Base/index.md"
    (served / rel).unlink()

    result = preserve.preserve(
        served, scope="Survey", category="records", filename="tide.txt",
        content="High tide at noon.", today=dt.date(2026, 1, 2),
    )

    with read.snapshot(served) as view:
        assert result.path.removeprefix("Knowledge Base/") in view.page(rel).body.decode("utf-8")
    assert not any("index.md missing" in warning for warning in result.warnings)


@pytest.mark.parametrize("backend", ["file", "store"])
def test_preserve_refuses_a_top_index_changed_before_commit(backend, request, monkeypatch):
    """A concurrent activity update must refuse preserve without publishing its evidence."""
    root = request.getfixturevalue("served" if backend == "store" else "vault")
    rel = "Knowledge Base/index.md"
    path = root / rel
    current, _guard = vault_module.read_guarded_text(root, path)
    concurrent = current + "\n- 2026-01-02: Captured the estuary survey.\n"
    commit = preserve.batch_atomic_write

    def change_before_commit(writes, **kwargs):
        if backend == "store":
            store_writer = request.getfixturevalue("writer")
            write.commit(store_writer, write.StoreChange(
                write.Origin.GOVERNED, "test",
                puts=(write.PagePut(rel, concurrent.encode("utf-8")),),
            ))
        else:
            path.write_text(concurrent, encoding="utf-8")
        return commit(writes, **kwargs)

    monkeypatch.setattr(preserve, "batch_atomic_write", change_before_commit)

    with pytest.raises(vault_module.ContentHashMismatchError):
        preserve.preserve(
            root, scope="Survey", category="records", filename="tide.txt",
            content="High tide at noon.", today=dt.date(2026, 1, 2),
        )

    assert vault_module.read_guarded_text(root, path)[0] == concurrent
    assert not read.holds_file(root, "Knowledge Base/Evidence/Survey/records/tide.txt.md")
    assert not (root / "Knowledge Base/Evidence/Survey/records/tide.txt").exists()
