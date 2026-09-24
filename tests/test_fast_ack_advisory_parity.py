"""Under fast acknowledgement the deferred advisory IS the inline advisory.

With ``EXOMEM_FAST_DURABLE_ACK=1`` a `remember` or an `edit` no longer sweeps
for near-duplicates and overlaps on the request thread: the receipt's
`write_advisory` component runs the sweep in the background and the terminal
carries an exact `advisory_result_ref`. That hand-off is only honest if the
deferred result is exactly what the same write returns inline with the flag
off -- per route, from the same function with the same inputs:

* `remember`: the draft's title and body, the same-type duplicate filter, the
  post-commit reuse of the page's published vectors, and the same tie order;
* `edit`: the new body's bare paragraphs, overlaps only;
* a vault-scope write: the same warnings.

This is the differential. Two copies of one generic corpus take the same
writes, once with the flag off (inline warnings) and once with it on (warnings
read back through the exact result reference after one drain pass). The
encoder is a deterministic bag-of-words stand-in, so equality is exact and
needs no model. The corpus is built so that every divergence a looser deferred
sweep would have -- another type's twin, a heading that differs from the
title, identical pages tied at cosine 1.0, an edit that lands in the
duplicate band, a page outside the knowledge base -- changes the warnings.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import FIXTURE_VAULT, initialize_vault_state_offline

from exomem import derived_drain, derived_receipts, lexstore, pending_recall, writer_lease
from exomem import edit as edit_module
from exomem import find as find_module
from exomem import note as note_module

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="SQLite build lacks FTS5"
)

_W = (
    "harbor ferry lighthouse tide anchor mooring rigging keel rudder sail "
    "garden soil compost seedling harvest irrigation pruning mulch trellis "
    "orchard melody rhythm tempo chord harmony cadence timbre octave refrain "
    "quartz basalt granite obsidian pumice slate marble comet nebula photon "
    "orbit aperture spectrum eclipse gravity"
).split()
_BASE = " ".join(_W[:12])
_TWIN = " ".join(_W[12:22])
_FAIL = " ".join(_W[22:32])
_OUTSIDE = " ".join(_W[32:])
_REF = re.compile(r"exomem://review/write-advisory/[0-9A-Za-z_-]+")
_FINGERPRINT = re.compile(r"fingerprint: [0-9a-f]+")


def _page(title: str, body: str, *, page_type: str = "insight", status: str = "active") -> str:
    return (
        f"---\ntype: {page_type}\nstatus: {status}\ncreated: 2026-05-10\n"
        "updated: 2026-05-10\nsources: []\ntags: [parity]\n---\n\n"
        f"# {title}\n\n{body}\n"
    )


def _word_vector(text: str) -> np.ndarray:
    from exomem import embeddings

    total = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    for word in re.findall(r"[a-z]+", text.lower()):
        seed = int.from_bytes(hashlib.sha256(word.encode("utf-8")).digest()[:8], "little")
        total += np.random.default_rng(seed).standard_normal(embeddings.VECTOR_DIM).astype(
            np.float32
        )
    norm = float(np.linalg.norm(total)) or 1.0
    return total / norm


@pytest.fixture
def bag_of_words_encoder(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from exomem import embeddings, readiness

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(readiness, "should_defer", lambda *_a, **_k: False)
    calls: list[int] = []

    def encode(texts, *, is_query: bool = False):  # noqa: ANN001
        calls.append(len(texts))
        return np.stack([_word_vector(text) for text in texts]).astype(np.float32)

    monkeypatch.setattr(embeddings, "embed_texts", encode)
    monkeypatch.setattr(embeddings, "_embed_live_chunks", lambda chunks: encode(list(chunks)))
    # A wide overlap band so partial restatements land in it.
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", "0.55")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    return calls


def _seeded_vault(tmp_path: Path, name: str) -> Path:
    from exomem import embeddings

    root = tmp_path / name
    shutil.copytree(FIXTURE_VAULT, root)
    kb = root / "Knowledge Base" / "Notes"
    (kb / "Insights").mkdir(parents=True, exist_ok=True)
    (kb / "Failures").mkdir(parents=True, exist_ok=True)
    (kb / "Insights" / "parity-base.md").write_text(_page("Parity base", _BASE), "utf-8")
    for twin in "abcd":
        (kb / "Insights" / f"parity-twin-{twin}.md").write_text(
            _page("Parity twin", _TWIN), "utf-8"
        )
    (kb / "Failures" / "parity-failure.md").write_text(
        _page("Parity failure", _FAIL, page_type="failure"), "utf-8"
    )
    (kb / "Insights" / "parity-edit-target.md").write_text(
        _page("Parity edit target", "eclipse gravity orbit", status="draft"), "utf-8"
    )
    (root / "Other").mkdir(exist_ok=True)
    (root / "Other" / "parity-outside.md").write_text(_page("Parity outside", _OUTSIDE), "utf-8")
    initialize_vault_state_offline(root, source="advisory parity fixture")
    find_module.clear_cache()
    embeddings.clear_embedding_indexes()
    # Index the whole vault, so the vault-scope write has a counterpart outside
    # the knowledge base; knowledge-base sweeps still admit only their scope.
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setenv("EXOMEM_INDEX_SCOPE", "vault")
        embeddings.get_embedding_index(root).rebuild_all()
    assert lexstore.get_store(root).rebuild_atomic() is True
    return root


def _advisory_only(warnings: list[str]) -> list[str]:
    """Advisory warnings in returned order, with per-run ids normalized."""
    return [
        _FINGERPRINT.sub("fingerprint: <fp>", _REF.sub("<review-ref>", warning))
        for warning in warnings
        if "exomem://review/write-advisory/" in warning
    ]


def _writes() -> list[tuple[str, str, dict, dict]]:
    """(label, command, leaf kwargs, env) in a fixed order."""
    note = {"note_type": "insight", "status": "draft"}
    return [
        # Four identical pages tie at cosine 1.0; only three fit `top_n`.
        ("twin-tie", "remember", {**note, "title": "Parity twin", "content": _TWIN}, {}),
        # A failure's exact twin: the remember sweep only flags its own type.
        ("other-type-twin", "remember", {**note, "title": "Parity failure", "content": _FAIL}, {}),
        # A heading that differs from the title is one of the draft's chunks.
        (
            "heading-differs",
            "remember",
            {**note, "title": "Parity heading", "content": "# Garden notes\n\n" + _BASE},
            {},
        ),
        # The edit lands in the duplicate band of the base page: the edit
        # sweep scores bare paragraphs for overlaps only.
        (
            "edit-into-dup-band",
            "edit_memory",
            {"path": "Knowledge Base/Notes/Insights/parity-edit-target.md", "why": "parity",
             "new_body": "# Parity edit target\n\n" + _BASE + "\n\n" + " ".join(_W[:8])},
            {},
        ),
        # Outside the knowledge base, visible only under the vault scope.
        (
            "vault-scope",
            "remember",
            {**note, "title": "Parity outside", "content": _OUTSIDE},
            {"EXOMEM_INDEX_SCOPE": "vault"},
        ),
    ]


def _run(tmp_path: Path, root: Path, *, fast: bool, monkeypatch) -> list[dict]:
    if fast:
        monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    else:
        monkeypatch.delenv("EXOMEM_FAST_DURABLE_ACK", raising=False)
    pending_recall.reset()
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / f"lease-{root.name}")
    )
    out = []
    for label, command, kwargs, env in _writes():
        returned: list[str] = []

        def leaf(vault_root: Path, command=command, kwargs=kwargs, returned=returned, **_s):
            if command == "remember":
                result = note_module.note(vault_root, **kwargs)
            else:
                result = edit_module.edit(vault_root, **kwargs)
            returned.extend(result.warnings)
            return {"path": result.path, "warnings": list(result.warnings)}

        with monkeypatch.context() as scoped:
            for key, value in env.items():
                scoped.setenv(key, value)
            terminal = manager.invoke(
                SimpleNamespace(name=command, leaf=leaf, read_only=False),
                (root,),
                {"response_detail": "compact"},
                idempotency_key=None,
                mutation_request_id=str(uuid.uuid4()),
            )
            assert terminal["status"] == "committed", (label, terminal)
            deferred = None
            if fast:
                derived_drain.drain_once(
                    root,
                    dispatch=derived_drain.component_dispatcher(),
                    observe_current_generation=derived_drain.canonical_generation_observer(),
                    visibility_publisher=pending_recall.publish,
                    retire_visibility=derived_drain.pending_visibility_retirer(),
                    limit=derived_drain.progress_limit(mode_name="normal"),
                    now=time.time(),
                )
                ref = terminal.get("advisory_result_ref")
                assert ref, (label, terminal)
                result = derived_receipts.read_advisory_result(root, ref)
                assert result is not None and result.state == "ready", (label, result)
                deferred = _advisory_only([c.warning for c in result.candidates])
        out.append({"label": label, "inline": _advisory_only(returned), "deferred": deferred})
    return out


@pytest.fixture
def both_runs(tmp_path: Path, bag_of_words_encoder: list[int], monkeypatch) -> tuple[list, list]:
    slow = _run(tmp_path, _seeded_vault(tmp_path, "slow"), fast=False, monkeypatch=monkeypatch)
    fast = _run(tmp_path, _seeded_vault(tmp_path, "fast"), fast=True, monkeypatch=monkeypatch)
    return slow, fast


def test_the_corpus_exercises_every_divergence(both_runs) -> None:
    """Equality below is not vacuous: each write has something to get wrong."""
    slow, _fast = both_runs
    by = {row["label"]: row["inline"] for row in slow}
    assert len(by["twin-tie"]) == 3 and all("near-duplicate" in w for w in by["twin-tie"])
    assert not any("parity-failure" in w for w in by["other-type-twin"]), by
    assert by["heading-differs"], by
    assert by["edit-into-dup-band"] == [] or all(
        "overlaps active note" in w for w in by["edit-into-dup-band"]
    ), by
    assert any("parity-outside" in w for w in by["vault-scope"]), by


def test_fast_ack_computes_no_inline_sweep_for_remember_or_edit(both_runs) -> None:
    _slow, fast = both_runs
    assert {row["label"]: row["inline"] for row in fast} == {
        row["label"]: [] for row in fast
    }


def test_deferred_advisory_equals_the_inline_advisory_per_route(both_runs) -> None:
    slow, fast = both_runs
    assert {row["label"]: row["deferred"] for row in fast} == {
        row["label"]: row["inline"] for row in slow
    }


def _leased(tmp_path: Path, root: Path, command: str, call) -> dict:
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))

    def leaf(vault_root: Path, **_surface):
        result = call(vault_root)
        return {"path": result.path, "warnings": list(result.warnings)}

    return manager.invoke(
        SimpleNamespace(name=command, leaf=leaf, read_only=False),
        (root,),
        {"response_detail": "compact"},
        idempotency_key=None,
        mutation_request_id=str(uuid.uuid4()),
    )


def test_an_edit_that_leaves_the_body_alone_takes_no_advisory_custody(
    tmp_path: Path, bag_of_words_encoder: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inline, a tags-only edit sweeps nothing; deferred, there is no job."""
    root = _seeded_vault(tmp_path, "tags")
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    terminal = _leased(
        tmp_path,
        root,
        "edit_memory",
        lambda vault_root: edit_module.edit(
            vault_root,
            path="Knowledge Base/Notes/Insights/parity-edit-target.md",
            why="parity",
            tags=["parity", "retagged"],
        ),
    )

    assert terminal["status"] == "committed"
    assert terminal["advisory_sync"] == "not_required"
    assert "advisory_result_ref" not in terminal


def test_lost_route_inputs_fall_back_to_the_generic_component(
    tmp_path: Path, bag_of_words_encoder: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that lost the draft still converges the job, never strands it.

    The route's inputs live only in the process that committed the write. If
    that process is gone when the component runs, the component falls back to
    its generic sweep over the page's published vectors rather than leaving
    the job pending.
    """
    from exomem import advisory_handoff

    root = _seeded_vault(tmp_path, "lost")
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    terminal = _leased(
        tmp_path,
        root,
        "remember",
        lambda vault_root: note_module.note(
            vault_root, content=_TWIN, note_type="insight", title="Parity lost", status="draft"
        ),
    )
    assert terminal["advisory_sync"] == "pending"
    advisory_handoff.reset_route_inputs()

    derived_drain.drain_once(
        root,
        dispatch=derived_drain.component_dispatcher(),
        observe_current_generation=derived_drain.canonical_generation_observer(),
        visibility_publisher=pending_recall.publish,
        retire_visibility=derived_drain.pending_visibility_retirer(),
        limit=derived_drain.progress_limit(mode_name="normal"),
        now=time.time(),
    )

    result = derived_receipts.read_advisory_result(root, terminal["advisory_result_ref"])
    assert result is not None and result.state == "ready", result
    assert result.candidates
