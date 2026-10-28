"""A turn that resolves nothing may list pages two readings agree on.

A recurring subject in a long vault ("thin slices for marrowfen cure") names no
anchor, carries no page and echoes no title, so activation used to abstain with
nothing listed. The related listing adds `related` to that abstention when a
page is both a current row of the turn's full-subject lexical window and has a
stored recall chunk that clears the corpus-relative band against the turn
vector the anchor lane already encoded. Nothing is served for a listed page.

The fixtures plant the vectors: the recall encoder is out of the loop, the
vault, the catalogue, both sidecars, the band, the resolver and egress are real.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from test_activation_signature_evidence import _plant_background
from test_governance_egress import _external, write_rule, write_scope
from test_working_set_egress import _prepare_end_to_end_vault, _reset_governance_state
from test_working_set_index import _seed_structure, _write

from exomem import (
    commands,
    embedding_index,
    embeddings,
    index_paths,
    lexstore,
    readiness,
    recall_space,
    working_set,
    working_set_index,
    working_set_resolve,
    working_set_runtime,
)
from exomem.governance.principal import request_scope

# These tests exercise the `anchors` packet, the rollback mode; `maps` is the shipped default.
pytestmark = [pytest.mark.timeout(600), pytest.mark.usefixtures("anchors_packet")]

DIM = embeddings.VECTOR_DIM
AXIS = np.eye(DIM, dtype=np.float32)[0]
OFF_AXIS = np.eye(DIM, dtype=np.float32)[1]
FINGERPRINT = "planted-encoder|cls|l2|0000"
TURN = "thin slices for marrowfen cure"
NOTES = "Knowledge Base/Notes"
BODY = "Thin slices for the marrowfen cure rest overnight under salt."
UNRELATED_BODY = "The rack holds the winter crates until spring."


def _random_unit(text: str) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "little")
    vector = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return vector / np.linalg.norm(vector)


def _toward_turn(cosine: float) -> np.ndarray:
    """A unit vector whose cosine with the turn vector is `cosine`."""
    return (cosine * AXIS + math.sqrt(1.0 - cosine * cosine) * OFF_AXIS).astype(np.float32)


def _passages(texts: list[str]) -> np.ndarray:
    return np.vstack([_random_unit(text) for text in texts])


def _note(vault: Path, title: str, body: str) -> str:
    rel = f"{NOTES}/{title}.md"
    _write(
        vault / rel,
        f"---\ntype: note\nstatus: active\n---\n# {title}\n\n{body}\n",
    )
    return rel


def _planted_vault(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[str, tuple[str, np.ndarray]],
    *,
    chunk_fingerprint: str = FINGERPRINT,
    resident: bool = True,
) -> dict[str, str]:
    """A vault of 50 background pages plus `pages` (title -> body, chunk vector).

    The activation encoder gives every anchor signature a random direction, so
    no anchor bands and the turn stays unresolved. The recall sidecar holds one
    random chunk per background page and one planted chunk per page in `pages`.
    Returns title -> path.
    """
    _seed_structure(vault)
    background = _plant_background(vault)
    paths = {title: _note(vault, title, body) for title, (body, _vector) in pages.items()}

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "embed_activation_passages", _passages)
    monkeypatch.setattr(embeddings, "embed_activation_passages_if_loaded", _passages)
    monkeypatch.setattr(embeddings, "activation_fingerprint", lambda: FINGERPRINT)
    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", lambda text: AXIS)
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)

    # The sidecar records the space of the encoder that wrote it.
    monkeypatch.setattr(recall_space, "resident_fingerprint", lambda model: chunk_fingerprint)
    recall = embeddings.get_embedding_index(vault)
    for page in background:
        rel = str(page.relative_to(vault))
        recall.upsert_file(rel, ["background chunk"], _random_unit(rel)[None, :], 1.0)
    for title, (_body, vector) in pages.items():
        recall.upsert_file(paths[title], ["planted chunk"], vector[None, :], 1.0)
    if resident:
        recall.all_vectors()
    else:
        recall.unload_cache()

    lexstore.ensure_fresh(vault)
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    assert len(index.vector_matrix(FINGERPRINT)[0]) >= 50
    index.close()
    working_set_runtime.reset_caches_for_tests()
    return paths


def _activate(vault: Path) -> dict:
    return commands.op_activate_context(vault, turn=TURN)


def _nothing_served(packet: dict) -> None:
    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unresolved"}
    assert packet["anchors"] == []
    assert packet["units"] == []
    assert packet["pointers"] == []
    assert packet["current_state"] == []


def _no_related_block(packet: dict) -> None:
    assert "related" not in packet
    assert {"role": "related", "reason": "lane_truncated"} not in packet["missing"]


def test_a_recurring_subject_is_listed_as_related_and_nothing_is_served(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _planted_vault(
        vault, monkeypatch, {"Brine ledger": (BODY, _toward_turn(1.0))}
    )

    packet = _activate(vault)

    _nothing_served(packet)
    assert packet["related"] == [
        {
            "path": paths["Brine ledger"],
            "title": "Brine ledger",
            "projects": [],
            "evidence": ["retrieval", "vector_band"],
        }
    ]
    assert packet["generation"]["related_evidence"] == "ready"
    assert "Brine ledger" not in str(packet["recent_context"])


def test_a_content_free_turn_lists_nothing_and_never_reads_the_matrix(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _planted_vault(vault, monkeypatch, {"Brine ledger": (BODY, _toward_turn(1.0))})
    recall = embeddings.get_embedding_index(vault)
    monkeypatch.setattr(
        type(recall),
        "resident_matrix",
        lambda *_a, **_k: pytest.fail("the matrix was read for a turn with no content word"),
    )

    packet = commands.op_activate_context(vault, turn="Could you do that now?")

    _no_related_block(packet)
    assert "related_evidence" not in packet["generation"]


def test_a_page_that_bands_without_lexical_support_is_not_listed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _planted_vault(
        vault,
        monkeypatch,
        {
            "Brine ledger": (BODY, _random_unit("Brine ledger")),
            # It sits on the turn's axis but shares no word with the turn.
            "Crate inventory": (UNRELATED_BODY, _toward_turn(1.0)),
        },
    )

    packet = _activate(vault)

    _nothing_served(packet)
    _no_related_block(packet)
    assert packet["generation"]["related_evidence"] == "ready"


def test_a_full_lexical_window_without_a_band_lists_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = working_set.RETRIEVAL_CARRY_FETCH
    pages = {
        f"Salt log {i:02d}": (BODY, _random_unit(f"Salt log {i:02d}")) for i in range(window + 2)
    }
    _planted_vault(vault, monkeypatch, pages)

    packet = _activate(vault)

    _nothing_served(packet)
    _no_related_block(packet)
    assert packet["generation"]["related_evidence"] == "ready"


def test_a_restricted_reader_gets_todays_packet_and_no_similarity_is_computed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _planted_vault(
        vault, monkeypatch, {"Brine ledger": (BODY, _toward_turn(1.0))}
    )
    write_scope(vault, paths="Knowledge Base/Notes/Secret/*")
    write_rule(vault, ceiling=0, audience="external")
    _prepare_end_to_end_vault(vault)
    _reset_governance_state(vault)
    with request_scope(_external()):
        # Armed after the fixture and the index rebuild: only the request is watched.
        recall = embeddings.get_embedding_index(vault)
        monkeypatch.setattr(
            type(recall),
            "resident_matrix",
            lambda *_a, **_k: pytest.fail("the matrix was read for a restricted reader"),
        )
        monkeypatch.setattr(
            working_set_resolve,
            "semantic_band",
            lambda *_a, **_k: pytest.fail("a band was computed for a restricted reader"),
        )
        packet = _activate(vault)

    _nothing_served(packet)
    _no_related_block(packet)
    assert packet["generation"]["related_evidence"] == "audience_restricted"
    assert paths["Brine ledger"] not in str(packet)


def test_a_cold_matrix_is_never_loaded_for_the_listing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _planted_vault(
        vault, monkeypatch, {"Brine ledger": (BODY, _toward_turn(1.0))}, resident=False
    )
    recall = embeddings.get_embedding_index(vault)
    for name in ("_load_all_rows", "require_resident_matrix", "all_vectors", "start_corpus_matrix_load"):
        monkeypatch.setattr(
            recall, name, lambda *_a, _name=name, **_k: pytest.fail(f"activation called {_name}")
        )

    packet = _activate(vault)

    _nothing_served(packet)
    _no_related_block(packet)
    assert packet["generation"]["related_evidence"] == "cold"


def test_a_resolved_turn_skips_the_listing(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _planted_vault(vault, monkeypatch, {"Brine ledger": (BODY, _toward_turn(1.0))})
    index = working_set_index.WorkingSetIndex(vault)
    chosen = next(row for row in index.anchors() if row.title == "Cargo Sled")
    index.close()
    recall = embeddings.get_embedding_index(vault)
    monkeypatch.setattr(
        type(recall),
        "resident_matrix",
        lambda *_a, **_k: pytest.fail("the matrix was read for a resolved turn"),
    )

    packet = commands.op_activate_context(vault, turn=TURN, anchor=chosen.ref or chosen.path)

    assert packet["abstained"] is False
    assert "related" not in packet
    assert "related_evidence" not in packet["generation"]


def test_a_matrix_from_another_space_is_not_compared(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _planted_vault(
        vault,
        monkeypatch,
        {"Brine ledger": (BODY, _toward_turn(1.0))},
        chunk_fingerprint="another-encoder|cls|l2|0000",
    )

    packet = _activate(vault)

    _nothing_served(packet)
    _no_related_block(packet)
    assert packet["generation"]["related_evidence"] == "space_mismatch"


def test_the_listing_is_capped_at_three_in_similarity_order_and_says_it_was_cut(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = {
        "Brine ledger": (BODY, _toward_turn(0.7)),
        "Salt log": (BODY, _toward_turn(0.9)),
        "Rack diary": (BODY, _toward_turn(0.8)),
        "Cure notes": (BODY, _toward_turn(0.6)),
    }
    paths = _planted_vault(vault, monkeypatch, pages)

    packet = _activate(vault)

    assert [entry["title"] for entry in packet["related"]] == ["Salt log", "Rack diary", "Brine ledger"]
    assert {"role": "related", "reason": "lane_truncated"} in packet["missing"]
    assert paths["Cure notes"] not in str(packet["related"])
    assert not any(isinstance(value, float) for entry in packet["related"] for value in entry.values())


def test_the_packet_cache_follows_the_resident_matrix(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _planted_vault(
        vault, monkeypatch, {"Brine ledger": (BODY, _toward_turn(1.0))}
    )
    recall = embeddings.get_embedding_index(vault)

    def served() -> dict:
        return working_set_runtime.serve(vault, turn=TURN, max_chars=4000)

    first = served()
    recall.unload_cache()
    while_cold = served()
    recall.all_vectors()
    warm_again = served()

    assert [entry["path"] for entry in first["related"]] == [paths["Brine ledger"]]
    assert while_cold["generation"]["related_evidence"] == "cold"
    assert "related" not in while_cold
    assert [entry["path"] for entry in warm_again["related"]] == [paths["Brine ledger"]]


def test_a_chunk_publication_invalidates_a_previously_related_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A derived writer can finish after the lexical catalogue already caught up."""
    paths = _planted_vault(
        vault, monkeypatch, {"Brine ledger": (BODY, _toward_turn(1.0))}
    )
    first = working_set_runtime.serve(vault, turn=TURN, max_chars=4000)
    assert [entry["path"] for entry in first["related"]] == [paths["Brine ledger"]]

    recall = embeddings.get_embedding_index(vault)
    writer = embedding_index.EmbeddingIndex(vault, path=recall.path)
    writer.upsert_file(
        paths["Brine ledger"], ["published chunk"], _random_unit("new publication")[None, :], 2.0
    )

    packet = working_set_runtime.serve(vault, turn=TURN, max_chars=4000)

    _nothing_served(packet)
    _no_related_block(packet)
    assert packet["generation"]["related_evidence"] == "cold"


def test_a_busy_index_registry_does_not_delay_related_activation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrelated index registry holder must not stall the public activation."""
    paths = _planted_vault(
        vault, monkeypatch, {"Brine ledger": (BODY, _toward_turn(1.0))}
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        embeddings._INDEX_CACHE_LOCK.acquire()
        try:
            # The deadline bounds a deadlock reproduction, not activation performance.
            packet = executor.submit(_activate, vault).result(timeout=3)
        finally:
            embeddings._INDEX_CACHE_LOCK.release()

    _nothing_served(packet)
    assert [entry["path"] for entry in packet["related"]] == [paths["Brine ledger"]]


def test_a_locked_chunk_publication_lists_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing cannot use a matrix whose publication a writer prevents it verifying."""
    _planted_vault(vault, monkeypatch, {"Brine ledger": (BODY, _toward_turn(1.0))})
    recall = embeddings.get_embedding_index(vault)
    with sqlite3.connect(recall.path) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        try:
            packet = _activate(vault)
        finally:
            writer.rollback()

    _nothing_served(packet)
    _no_related_block(packet)
    assert packet["generation"]["related_evidence"] == "cold"


def test_a_listing_does_not_recreate_a_sidecar_removed_during_its_read(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent sidecar retirement must not leave a new empty database behind."""
    _planted_vault(vault, monkeypatch, {"Brine ledger": (BODY, _toward_turn(1.0))})
    recall = embeddings.get_embedding_index(vault)
    connect = sqlite3.connect
    removed = False

    def remove_before_connect(database, *args, **kwargs):
        nonlocal removed
        if not removed and database in (
            recall.path, str(recall.path), f"{recall.path.resolve().as_uri()}?mode=ro"
        ):
            recall.path.unlink()
            removed = True
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", remove_before_connect)

    packet = _activate(vault)

    assert removed
    assert not recall.path.exists()
    _nothing_served(packet)
    _no_related_block(packet)
    assert packet["generation"]["related_evidence"] == "cold"


def test_an_unreadable_cutover_pointer_does_not_revive_the_old_matrix(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable cutover must not make the retained legacy matrix serve again."""
    _planted_vault(vault, monkeypatch, {"Brine ledger": (BODY, _toward_turn(1.0))})
    legacy = index_paths.legacy_sidecar_path(vault)
    pointer = legacy.parent / index_paths.ACTIVE_SIDECAR_POINTER
    index_paths.publish_active_sidecar(vault, index_paths.space_sidecar_name("next space"))
    open_file = index_paths.os.open

    def deny_pointer(path, *args, **kwargs):
        if Path(path) == pointer:
            raise PermissionError("the serving pointer is temporarily unreadable")
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(index_paths.os, "open", deny_pointer)

    packet = _activate(vault)

    _nothing_served(packet)
    _no_related_block(packet)
    assert packet["generation"]["related_evidence"] == "cold"
    assert index_paths.sidecar_path(vault) == legacy
