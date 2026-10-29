"""Derived sync keeps the context catalogue current after a write.

The workflow under test is a write whose receipt reports the catalogue
component complete: the catalogue then holds what a fresh build of the vault
would. Moves and deletes name neither path in their receipts, so only a real
command shows whether the old entry went.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import _drain_background_threads
from test_context_catalogue import KB, _built, _seed, _write
from test_planning_mutation import _manifest

from exomem import (
    commands,
    deferred_index,
    derived_drain,
    derived_receipts,
    epistemic_graph,
    lexstore,
    pending_recall,
    planning,
    writer_lease,
)
from exomem import context_catalogue as cc
from exomem import find as find_module
from exomem.derived_receipts import DerivedComponent

pytestmark = pytest.mark.skipif(not lexstore.fts5_available(), reason="SQLite build lacks FTS5")

NOTES = f"{KB}/Projects/Harbour"
MANIFEST = f"{KB}/Planning/Corridor/_collection.md"


@pytest.fixture(autouse=True)
def _fast_ack_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    pending_recall.reset()
    find_module.clear_cache()


def _batch_ids(vault: Path) -> list[str]:
    connection = sqlite3.connect(deferred_index.store_path(vault))
    try:
        return [str(batch_id) for (batch_id,) in connection.execute("SELECT batch_id FROM derived_batches ORDER BY rowid")]
    except sqlite3.OperationalError:
        return []  # the schema is created by the first prepare
    finally:
        connection.close()


def _command(tmp_path: Path, vault: Path, name: str, run, serial: int) -> list[str]:
    """One real write command through the lease and receipt seams; the batches it prepared."""
    before = set(_batch_ids(vault))
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease-state"))
    terminal = manager.invoke(
        SimpleNamespace(name=name, leaf=run, read_only=False),
        (vault,),
        {"response_detail": "compact"},
        idempotency_key=None,
        mutation_request_id=f"{serial:08d}-3333-4333-8333-333333333333",
    )
    assert terminal["status"] == "committed", terminal
    return [batch_id for batch_id in _batch_ids(vault) if batch_id not in before]


def _settle(vault: Path, batch_ids: list[str]) -> None:
    """Drain passes until every batch reports its catalogue component complete."""
    clock = time.time()
    for _pass in range(12):
        derived_drain.drain_once(
            vault,
            dispatch=derived_drain.component_dispatcher(),
            observe_current_generation=derived_drain.canonical_generation_observer(),
            visibility_publisher=pending_recall.publish,
            retire_visibility=derived_drain.pending_visibility_retirer(),
            limit=derived_drain.progress_limit(mode_name="normal"),
            now=clock,
        )
        # A graph rebuild a pass started finishes before the next pass; a retried component is due after its backoff.
        _drain_background_threads()
        if epistemic_graph.cache_token(vault) is None:
            epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()  # stands in for the running graph drain
        clock += 120.0
        states = {
            derived_receipts.component_status(
                vault, derived_receipts._load_receipt(vault, batch_id), DerivedComponent.CONTEXT_CATALOGUE
            ).state
            for batch_id in batch_ids
        }
        if states == {"completed"}:
            return
    pytest.fail(f"the catalogue component did not complete: {states}")


def _plain_pages(vault: Path) -> None:
    """Pages a write command may move or delete: no compiled type, so no semantic contract applies."""
    _write(
        vault,
        f"{NOTES}/dock-log.md",
        "---\nstatus: active\n---\n\n# Dock log\n\n## Relations\n\n"
        f"- member_of [[{NOTES}/berth-hub]]\n",
    )
    _write(vault, f"{NOTES}/berth-hub.md", "---\nstatus: active\ntopics: [harbour]\n---\n\n# Berth hub\n\nBody.\n")
    _write(vault, f"{NOTES}/loose-end.md", "---\nstatus: active\n---\n\n# Loose end\n\nBody.\n")


def _snapshot(catalogue: cc.ContextCatalogue):
    return catalogue.entries(), catalogue.membership_edges(), catalogue.pending()


def _fresh_build(vault: Path, tmp_path: Path, label: str):
    reference = cc.ContextCatalogue(vault, path=tmp_path / f"{label}.sqlite")
    reference.rebuild()
    return _snapshot(reference)


def test_the_catalogue_equals_a_fresh_build_after_each_write_the_drain_settles(vault: Path, tmp_path: Path) -> None:
    _seed(vault)
    _plain_pages(vault)
    catalogue = _built(vault)
    steps = [
        (
            "create-with-topics",
            lambda root: commands.op_create_file(
                root, f"{NOTES}/new-berth.md", "# New berth\n\nBody.\n", frontmatter={"status": "active", "topics": ["fleet"]}
            ),
        ),
        (
            "rename",
            lambda root: commands.op_move_file(root, f"{NOTES}/dock-log.md", f"{NOTES}/dock-ledger.md"),
        ),
        ("delete", lambda root: commands.op_delete(root, f"{NOTES}/loose-end.md", confirm=True)),
        (
            "collection-create",
            lambda root: planning.create_collection(root, MANIFEST, _manifest() + "\nCorridor planning items.\n", why="seed corridor"),
        ),
    ]
    for serial, (label, run) in enumerate(steps, start=1):
        batch_ids = _command(tmp_path, vault, label, run, serial)
        assert batch_ids, f"{label} prepared no receipt"
        _drain_background_threads()
        _settle(vault, batch_ids)
        assert _snapshot(catalogue) == _fresh_build(vault, tmp_path, label), label

    paths = {entry.path for entry in catalogue.entries()}
    assert f"{NOTES}/new-berth.md" in paths and MANIFEST in paths
    assert f"{NOTES}/dock-ledger.md" in paths
    assert f"{NOTES}/dock-log.md" not in paths  # the old ref does not linger
    assert f"{NOTES}/loose-end.md" not in paths
    assert catalogue.membership_edges().get(f"{NOTES}/dock-ledger.md") == (f"{NOTES}/berth-hub.md",)


def test_a_vault_without_a_built_catalogue_gets_none_from_a_write(vault: Path, tmp_path: Path) -> None:
    batch_ids = _command(
        tmp_path, vault, "create", lambda root: commands.op_create_file(
            root, f"{NOTES}/first.md", "# First\n\nBody.\n", frontmatter={"status": "active"}
        ), 1,
    )

    _settle(vault, batch_ids)

    assert not cc.sidecar_path(vault).exists()  # the first read builds it; a write does not


def test_a_write_completes_with_the_kill_switch_set_and_leaves_the_catalogue_alone(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault)
    catalogue = _built(vault)
    before = catalogue.entries()
    monkeypatch.setenv("EXOMEM_DISABLE_WORKING_SET", "1")

    batch_ids = _command(
        tmp_path, vault, "create", lambda root: commands.op_create_file(
            root, f"{NOTES}/first.md", "# First\n\nBody.\n", frontmatter={"status": "active"}
        ), 1,
    )
    _settle(vault, batch_ids)

    monkeypatch.delenv("EXOMEM_DISABLE_WORKING_SET")
    assert catalogue.entries() == before  # the component finished without opening the sidecar


def test_the_component_waits_for_a_graph_that_is_still_building(vault: Path) -> None:
    _seed(vault)
    _plain_pages(vault)
    catalogue = _built(vault)
    new = _write(
        vault, f"{NOTES}/second-dock.md",
        f"---\nstatus: active\n---\n\n# Second dock\n\n## Relations\n\n- member_of [[{NOTES}/berth-hub]]\n",
    )
    epistemic_graph.EpistemicGraphIndex(vault).path.unlink()  # the graph sidecar is being replaced

    assert cc.keep_current(vault) is False
    assert (f"{NOTES}/second-dock.md") not in catalogue.membership_edges()

    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    assert cc.keep_current(vault) is True
    assert catalogue.membership_edges()[new] == (f"{NOTES}/berth-hub.md",)


def test_a_request_path_fold_does_not_wait_for_another_readers_graph_proof(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proof of the graph sidecar has no timeout, so a fold that a request waits on must skip the pull while
    another reader proves it, keep the stored edges, and say the edges are warming."""
    _seed(vault)
    _plain_pages(vault)
    catalogue = _built(vault)
    page = f"{NOTES}/dock-log.md"
    _write(vault, page, "---\nstatus: active\n---\n\n# Dock log\n\nEdited.\n\n## Relations\n\n" + f"- member_of [[{NOTES}/berth-hub]]\n")
    real = epistemic_graph.cache_token
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()  # the graph moved, so the next fold must pull
    monkeypatch.setattr(
        epistemic_graph, "cache_token",
        lambda root, prove=True: epistemic_graph.UNPROVEN_TOKEN if prove == epistemic_graph.SINGLE_FLIGHT else real(root, prove=prove),
    )

    catalogue.update([page], prove=epistemic_graph.SINGLE_FLIGHT)

    assert catalogue.graph_state() == cc.EDGES_WARMING
    assert catalogue.membership_edges()[page] == (f"{NOTES}/berth-hub.md",)  # the stored edges stay
    catalogue.update([page])  # the background fold proves it
    assert catalogue.graph_state() == cc.EDGES_AVAILABLE


def test_the_graph_is_not_pulled_again_while_it_has_not_moved(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault)
    _plain_pages(vault)
    catalogue = _built(vault)
    pulls: list[str] = []
    pull = cc._membership_edges
    monkeypatch.setattr(cc, "_membership_edges", lambda root, prove=True: pulls.append("pull") or pull(root, prove))
    page = f"{NOTES}/dock-log.md"
    _write(vault, page, "---\nstatus: active\n---\n\n# Dock log\n\nEdited, relation kept.\n\n## Relations\n\n" + f"- member_of [[{NOTES}/berth-hub]]\n")

    catalogue.update([page])
    catalogue.update([page])

    assert pulls == []  # the graph token is where the first pull left it
    assert catalogue.membership_edges()[page] == (f"{NOTES}/berth-hub.md",)  # a replaced entry keeps its edges

    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    catalogue.update([page])
    catalogue.update([page])

    assert pulls == ["pull"]  # one pull for the moved graph, none for the unmoved one after it
