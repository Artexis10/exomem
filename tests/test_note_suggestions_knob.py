"""note(suggestions=) gates ONLY the related-links pass; dedupe stays on.

The corpus-aware block in note() runs two independent passes: the
link-suggestion query (a find-class nicety) and the near-dup/contradiction
embedding sweep (a dedupe GUARDRAIL the skill's discipline depends on).
`suggestions=` must gate the first and never the second.

The knob is **default-off** (issue #576). The suggestion pass is one whole
cold hybrid `find()` — 26 s of the measured 33 s advisory span at ~3k pages —
and it runs post-commit, where the write it follows has just moved every
freshness token the find cache is keyed on, so it can never hit warm. Worse,
its product does not reach the caller at all under the default
`response_detail="compact"` projection (`mutation_terminal.project_terminal`
only re-attaches the leaf under `detail="full"`). Paying it by default bought
a payload the default response discards. It stays fully available on request.

Because the block is now absent by default, its absence must be legible:
`write_feedback["suggestions"]["computed"]` distinguishes "asked, found none"
from "never asked", which a bare `related_pages: 0` could not.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from exomem import commands, corpus_aware
from exomem import note as note_module

_TOOL_SCHEMAS = Path(__file__).resolve().parent / "fixtures" / "mcp_tool_schemas.json"


@pytest.fixture
def corpus_spies(vault: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Enable the corpus block, spy on both passes, stub the heavy paths."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    calls = {"suggest": 0, "cosine": 0}

    def fake_suggest(*a, **k):
        calls["suggest"] += 1
        return []

    def fake_cosines(*a, **k):
        calls["cosine"] += 1
        return {}

    monkeypatch.setattr(corpus_aware, "suggest_related", fake_suggest)
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", fake_cosines)
    # Keep the post-write sidecar sync from touching the embedding model.
    from exomem import embeddings

    monkeypatch.setattr(embeddings, "upsert_after_write", lambda *a, **k: None)
    return calls


def test_default_skips_related_links_keeps_dedupe(
    vault: Path, corpus_spies: dict[str, int]
) -> None:
    """An interactive write must not pay the suggestion pass unasked."""
    note_module.note(
        vault,
        content="# Knob default probe\n\nBody.",
        note_type="insight",
        title="Knob default probe",
        status="draft",
    )
    assert corpus_spies == {"suggest": 0, "cosine": 1}


def test_explicit_suggestions_true_still_runs_related(
    vault: Path, corpus_spies: dict[str, int]
) -> None:
    """The pass is deferred to the caller, not removed."""
    note_module.note(
        vault,
        content="# Knob on probe\n\nBody.",
        note_type="insight",
        title="Knob on probe",
        suggestions=True,
        status="draft",
    )
    assert corpus_spies == {"suggest": 1, "cosine": 1}


def test_suggestions_false_skips_related_keeps_dedupe(
    vault: Path, corpus_spies: dict[str, int]
) -> None:
    note_module.note(
        vault,
        content="# Knob off probe\n\nBody.",
        note_type="insight",
        title="Knob off probe",
        suggestions=False,
        status="draft",
    )
    assert corpus_spies == {"suggest": 0, "cosine": 1}


def test_write_feedback_says_suggestions_were_not_computed(
    vault: Path, corpus_spies: dict[str, int]
) -> None:
    """`related_pages: 0` alone cannot distinguish "none" from "never ran"."""
    result = note_module.note(
        vault,
        content="# Feedback off probe\n\nBody.",
        note_type="insight",
        title="Feedback off probe",
        status="draft",
    ).as_dict()
    block = result["write_feedback"]["suggestions"]
    assert block["related_pages"] == 0
    assert block["computed"] is False
    # The route must be executable verbatim. Without `response_detail="full"`
    # a caller replaying it drops back to the compact projection, pays the
    # retrieval pass, and is handed neither the suggestions nor `computed`.
    assert block["route"] == {
        "tool": "remember",
        "args": {"suggestions": True, "response_detail": "full"},
    }


def test_write_feedback_says_suggestions_were_computed_when_requested(
    vault: Path, corpus_spies: dict[str, int]
) -> None:
    result = note_module.note(
        vault,
        content="# Feedback on probe\n\nBody.",
        note_type="insight",
        title="Feedback on probe",
        suggestions=True,
        status="draft",
    ).as_dict()
    block = result["write_feedback"]["suggestions"]
    assert block["computed"] is True
    assert "route" not in block


def test_the_advertised_route_actually_surfaces_the_block(
    vault: Path, corpus_spies: dict[str, int]
) -> None:
    """Replay the route we hand the caller and prove it returns something.

    `route` is only visible under `response_detail="full"`, but every write
    command defaults to compact. A route that omitted `response_detail` would
    read as complete, cost the caller a full retrieval pass on replay, and
    hand back nothing — so pin the projection, not just the dict's shape.
    """
    from exomem import mutation_terminal

    denied = note_module.note(
        vault,
        content="# Route probe off\n\nBody.",
        note_type="insight",
        title="Route probe off",
        status="draft",
    ).as_dict()
    route = denied["write_feedback"]["suggestions"]["route"]

    replayed = note_module.note(
        vault,
        content="# Route probe on\n\nBody.",
        note_type="insight",
        title="Route probe on",
        status="draft",
        **{k: v for k, v in route["args"].items() if k != "response_detail"},
    ).as_dict()
    # `_terminal` is the marker `project_terminal` gates on — without it the
    # projection silently returns its input unchanged, which would make this
    # test pass for the wrong reason.
    terminal = {
        "_terminal": mutation_terminal._TERMINAL_MARKER,
        "version": mutation_terminal._TERMINAL_VERSION,
        "ok": True,
        "state": "committed",
        "terminal": True,
        "mutated": True,
        "leaf_result": replayed,
        "warnings_count": 0,
    }

    projected = mutation_terminal.project_terminal(terminal, route["args"]["response_detail"])
    block = projected["diagnostics"]["write_feedback"]["suggestions"]
    assert block["computed"] is True

    # And the reason the route has to carry it at all.
    compact = mutation_terminal.project_terminal(terminal, "compact")
    assert "diagnostics" not in compact
    assert "write_feedback" not in compact
    assert "suggestions" not in compact


def test_remember_declares_the_new_default_on_its_tool_surface() -> None:
    """The client-visible `inputSchema` is what a caller discovers this from.

    Asserted on the pinned wire schema's `default` field, not on docstring
    prose — the prose is free to be reworded, the schema contract is not.
    """
    schema = json.loads(_TOOL_SCHEMAS.read_text(encoding="utf-8"))
    suggestions = schema["remember"]["inputSchema"]["properties"]["suggestions"]
    assert suggestions["default"] is False

    # Loose prose check: the description must still name the way back on.
    assert "suggestions=true" in suggestions["description"].lower()

    # Python defaults must agree with the wire schema, and the shared leaf with
    # both, or the CLI and MCP surfaces would diverge.
    assert inspect.signature(commands.op_remember).parameters["suggestions"].default is False
    assert inspect.signature(commands.op_note).parameters["suggestions"].default is False
    assert inspect.signature(note_module.note).parameters["suggestions"].default is False


# --------------------------------------------------------------------------- #
# Fast durable acknowledgement: the deferred component owns the sweep
# --------------------------------------------------------------------------- #


def _leased_note(tmp_path: Path, vault: Path, **kwargs) -> dict:
    """One `remember` through the real lease, as an MCP call routes it."""
    import uuid
    from types import SimpleNamespace

    from exomem import writer_lease

    def leaf(root: Path, **_surface_kwargs):
        result = note_module.note(root, **kwargs)
        return {"path": result.path, "warnings": list(result.warnings)}

    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "lease-state")
    )
    return manager.invoke(
        SimpleNamespace(name="remember", leaf=leaf, read_only=False),
        (vault,),
        {"response_detail": "compact", "suggestions": kwargs.get("suggestions", False)},
        idempotency_key=None,
        mutation_request_id=str(uuid.uuid4()),
    )


def test_fast_ack_delivers_the_dedupe_sweep_through_its_deferred_result(
    vault: Path,
    tmp_path: Path,
    corpus_spies: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under fast acknowledgement the sweep is not computed inline.

    The receipt's `write_advisory` component computes it in the background and
    the terminal carries its exact result reference instead, so an inline
    sweep would only double the work and delay the acknowledgement.
    """
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    terminal = _leased_note(
        tmp_path,
        vault,
        content="# Fast knob probe\n\nBody.",
        note_type="insight",
        title="Fast knob probe",
        status="draft",
    )

    assert terminal["status"] == "committed"
    assert terminal["advisory_sync"] == "pending"
    assert terminal["advisory_result_ref"].startswith("exomem://write-advisory-result/")
    assert corpus_spies == {"suggest": 0, "cosine": 0}


def test_fast_ack_keeps_explicit_suggestions_synchronous(
    vault: Path,
    tmp_path: Path,
    corpus_spies: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`suggestions=true` stays the explicit synchronous opt-in, sweep included."""
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    _leased_note(
        tmp_path,
        vault,
        content="# Fast knob on probe\n\nBody.",
        note_type="insight",
        title="Fast knob on probe",
        suggestions=True,
        status="draft",
    )

    assert corpus_spies == {"suggest": 1, "cosine": 1}


def test_fast_ack_off_keeps_the_inline_sweep(
    vault: Path,
    tmp_path: Path,
    corpus_spies: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag off, a leased write sweeps inline exactly as before."""
    monkeypatch.delenv("EXOMEM_FAST_DURABLE_ACK", raising=False)
    terminal = _leased_note(
        tmp_path,
        vault,
        content="# Slow knob probe\n\nBody.",
        note_type="insight",
        title="Slow knob probe",
        status="draft",
    )

    assert terminal["status"] == "committed"
    assert "advisory_result_ref" not in terminal
    assert corpus_spies == {"suggest": 0, "cosine": 1}
