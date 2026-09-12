"""Role support is represented, current, and relative to the reader."""

from pathlib import Path

import pytest
from test_artifact_role_review import ORIGIN, method, pending

from exomem import audit, due_state, find, semantic_contract

FAMILIES = ["artifact_role_promotion", "transient_state_review"]
DEST = "Knowledge Base/Notes/Custom/method.md"


def write(root, body, path=ORIGIN, kind="experiment", status="active"):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\ntype: {kind}\ntitle: {Path(path).stem}\nstatus: {status}\n---\n{body}")
    find.clear_cache()
    semantic_contract.reset_corpus_context_cache()
    return path


def extracted(content="Reusable method: stir twice.", refs=True):
    links = (
        (
            f"\n- relations: derived_from: [[{ORIGIN}#method]]\n"
            f"- relations: supports: [[{ORIGIN}#outcome]]\n"
        )
        if refs
        else ""
    )
    return f"## Procedure\n- id: extracted\n{links}\n{content}\n"


def report(root):
    return audit.audit(root, categories=FAMILIES)


def test_actual_audit_and_partial_destination_coverage(tmp_path):
    write(tmp_path, method())
    before = report(tmp_path)
    assert [f.category for f in before.findings] == ["artifact_role_promotion"]
    write(tmp_path, extracted(), DEST, "pattern")
    assert not report(tmp_path).findings
    assert "Reusable method" in (tmp_path / ORIGIN).read_text()
    write(tmp_path, extracted("Reusable method: stir THREE times."), DEST, "pattern")
    assert len(report(tmp_path).findings) == 1


@pytest.mark.parametrize(
    "body,kind",
    [
        (extracted(refs=False), "pattern"),
        (extracted(), "experiment"),
        (extracted(), "unknown-kind"),
        (extracted().replace("#method", "#missing"), "pattern"),
        (extracted().replace("stir twice", "STIR twice"), "pattern"),
    ],
)
def test_unrelated_or_inexact_destinations_do_not_cover(tmp_path, body, kind):
    write(tmp_path, method())
    write(tmp_path, body, DEST, kind)
    assert len(report(tmp_path).findings) == 1


def test_projection_delta_settles_and_reopens_destination_without_reconcile(tmp_path):
    write(tmp_path, method())
    due_state.reconcile(tmp_path)
    assert due_state.served(tmp_path)["categories"]["artifact_role_promotion"] == 1
    write(tmp_path, extracted(), DEST, "pattern")
    due_state.apply_write_delta(tmp_path, DEST)
    assert due_state.served(tmp_path) is None
    (tmp_path / DEST).unlink()
    find.clear_cache()
    due_state.apply_write_delta(tmp_path, DEST)
    assert due_state.served(tmp_path)["categories"]["artifact_role_promotion"] == 1


def test_explicit_coverage_exists_even_without_findings(tmp_path):
    write(tmp_path, "## Claim\nOrdinary text.")
    assert report(tmp_path).as_dict()["meta"]["coverage"] == dict.fromkeys(FAMILIES, "complete")


def test_current_state_uses_actual_projection_and_local_correction(tmp_path):
    write(tmp_path, pending())
    due_state.reconcile(tmp_path)
    assert due_state.served(tmp_path)["categories"]["transient_state_review"] == 1
    write(tmp_path, pending(text="Previously no taste results yet."))
    due_state.apply_write_delta(tmp_path, ORIGIN)
    assert due_state.served(tmp_path) is None


def test_private_destination_cannot_settle_public_candidate(tmp_path, monkeypatch):
    from exomem.governance import egress

    write(tmp_path, method())
    write(tmp_path, extracted(), DEST, "pattern")
    due_state.reconcile(tmp_path)
    monkeypatch.setattr(egress, "release_walk_filter", lambda *a, **k: lambda path: path != DEST)
    assert due_state.served(tmp_path)["categories"]["artifact_role_promotion"] == 1


def test_ninth_visible_support_survives_eight_private_supports(tmp_path, monkeypatch):
    from exomem.governance import egress

    write(tmp_path, method())
    for i in range(9):
        write(tmp_path, extracted(), f"Knowledge Base/Notes/Custom/support-{i}.md", "pattern")
    due_state.reconcile(tmp_path)
    monkeypatch.setattr(
        egress,
        "release_walk_filter",
        lambda *a, **k: lambda path: "support-" not in path or "support-8" in path,
    )
    assert due_state.served(tmp_path) is None


def test_synthesis_visibility_and_aliases(tmp_path, monkeypatch):
    from exomem.governance import egress

    source_a = "Knowledge Base/Sources/a.md"
    source_b = "Knowledge Base/Sources/b.md"
    write(tmp_path, "Document A.", source_a, "source")
    write(tmp_path, "Document B.", source_b, "source")
    body = (
        "## Finding\n- id: synthesis\n- relations: derived_from: [[a]]\n- "
        "relations: derived_from: [[b]]\n\nTaken together, both sources "
        "support cooling."
    )
    write(tmp_path, body)
    due_state.reconcile(tmp_path)
    assert due_state.served(tmp_path)["categories"]["artifact_role_promotion"] == 1
    monkeypatch.setattr(
        egress, "release_walk_filter", lambda *a, **k: lambda path: path != source_b
    )
    assert due_state.served(tmp_path) is None
    assert not report(tmp_path).findings


def test_stale_source_generation_omits_advice_until_reconcile(tmp_path):
    source_a, source_b = "Knowledge Base/Sources/a.md", "Knowledge Base/Sources/b.md"
    write(tmp_path, "Document A.", source_a, "source")
    write(tmp_path, "Document B.", source_b, "source")
    write(
        tmp_path,
        (
            "## Finding\n- id: synthesis\n- relations: derived_from: [[a]]\n- "
            "relations: derived_from: [[b]]\n\nTaken together, both sources "
            "support cooling."
        ),
    )
    due_state.reconcile(tmp_path)
    assert due_state.served(tmp_path)
    (tmp_path / source_b).write_text((tmp_path / source_b).read_text() + "\nChanged.")
    assert due_state.served(tmp_path) is None
    due_state.reconcile(tmp_path)
    assert due_state.served(tmp_path)


def test_epoch_overflow_never_enumerates_dependent_tail(tmp_path):
    from exomem import artifact_role_state as state

    write(tmp_path, method())
    index = report(tmp_path).role_state

    class BoundedDependents(dict):
        def __iter__(self):
            for i, value in enumerate(super().__iter__()):
                assert i < 8, "delta enumerated the dependent tail"
                yield value

    index["reverse"][ORIGIN] = BoundedDependents({f"origin-{i}": True for i in range(5000)})
    before = index["support_epoch"]
    updated, touched = state.delta(
        tmp_path, index, ORIGIN, find._CACHE.get(tmp_path / ORIGIN, tmp_path)
    )
    assert updated["support_epoch"] == before + 1
    assert len(touched) <= 9


def test_detector_exception_retains_commit_and_exposes_unknown(tmp_path, monkeypatch):
    from exomem import artifact_role_review as sensor

    write(tmp_path, pending())
    due_state.reconcile(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("injected detector failure")

    monkeypatch.setattr(sensor, "detect", broken)
    write(tmp_path, pending() + "\n## Claim\nUnrelated edit.")
    due_state.apply_write_delta(tmp_path, ORIGIN)
    assert due_state.served(tmp_path) is None
    assert report(tmp_path).as_dict()["meta"]["coverage"] == dict.fromkeys(FAMILIES, "unknown")
    assert "Unrelated edit." in (tmp_path / ORIGIN).read_text()


@pytest.mark.parametrize(
    "family,body", [("artifact_role_promotion", method()), ("transient_state_review", pending())]
)
def test_family_removal_probe_disables_delta_but_reconcile_heals(
    tmp_path, monkeypatch, family, body
):
    write(tmp_path, "## Claim\nOrdinary text.")
    due_state.reconcile(tmp_path)
    monkeypatch.setattr(
        due_state,
        "PAGE_DELTA_CATEGORIES",
        tuple(c for c in due_state.PAGE_DELTA_CATEGORIES if c != family),
    )
    write(tmp_path, body)
    due_state.apply_write_delta(tmp_path, ORIGIN)
    assert due_state.served(tmp_path) is None
    due_state.reconcile(tmp_path)
    assert due_state.served(tmp_path)["categories"][family] == 1


def test_due_delta_has_no_corpus_walk_or_destination_read(tmp_path, monkeypatch):
    write(tmp_path, method())
    write(tmp_path, extracted(), DEST, "pattern")
    due_state.reconcile(tmp_path)
    write(tmp_path, method(cue="Reusable method: stir three times."))
    original = Path.read_text

    def read(path, *a, **kw):
        assert path != tmp_path / DEST
        return original(path, *a, **kw)

    def forbidden(*a, **kw):
        pytest.fail("synchronous role delta requested a corpus walk")

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(find, "_walk_md", forbidden)
    monkeypatch.setattr(semantic_contract, "build_corpus_context", forbidden)
    due_state.apply_write_delta(tmp_path, ORIGIN)
    assert due_state.served(tmp_path)["categories"]["artifact_role_promotion"] == 1


def test_two_methods_settle_independently_and_duplicate_anchor_cannot_cover(tmp_path):
    second = (
        method(context="batch 2")
        .replace("id: method", "id: second")
        .replace("id: outcome", "id: second-outcome")
    )
    write(tmp_path, method() + second)
    write(tmp_path, extracted(), DEST, "pattern")
    assert len(report(tmp_path).findings) == 1
    write(tmp_path, method() + "\n## Claim\n- id: method\n\nDuplicate anchor.")
    assert len(report(tmp_path).findings) == 1


def test_internal_indentation_and_unicode_are_not_normalized_for_coverage(tmp_path):
    text = 'Reusable method: use Key.\n```\n  print("é")\n```'
    write(tmp_path, method(cue=text))
    write(tmp_path, extracted(text.replace("  print", "    print")), DEST, "pattern")
    assert len(report(tmp_path).findings) == 1
    write(tmp_path, extracted(text.replace("é", "e\u0301")), DEST, "pattern")
    assert len(report(tmp_path).findings) == 1
    write(tmp_path, extracted(text), DEST, "pattern")
    assert not report(tmp_path).findings


def test_exact_current_supersession_settles_without_deleting_history(tmp_path):
    write(
        tmp_path,
        pending()
        + (
            "\n## Claim\n- id: corrected\n"
            f"- relations: supersedes: [[{ORIGIN}#pending]]\n\n"
            "Previously no taste results existed."
        ),
    )
    assert not report(tmp_path).findings
    write(
        tmp_path,
        pending()
        + (
            "\n## Claim\n- id: corrected\n"
            f"- relations: supersedes: [[{ORIGIN}#missing]]\n\n"
            "Previously no taste results existed."
        ),
    )
    assert len(report(tmp_path).findings) == 1


def test_attention_uses_exact_triage_fingerprint_after_unrelated_edit(tmp_path):
    from exomem import attention, review_state

    write(tmp_path, method())
    item = attention.attention(tmp_path, categories=FAMILIES).items[0]
    store = review_state.ReviewStateStore(tmp_path)
    store.apply(
        item.item_id, item.fingerprint, action="dismiss", why="intentional: retained history"
    )
    write(tmp_path, method() + "\n## Claim\nUnrelated paragraph.")
    assert not attention.attention(tmp_path, categories=FAMILIES).items
    write(tmp_path, method(cue="Reusable method: stir three times."))
    assert attention.attention(tmp_path, categories=FAMILIES).items


def test_actual_observe_mutation_delivers_state_signal_and_correction_settles(vault):
    from exomem import commands, writer_lease

    write(
        vault,
        (
            "## Observations\n- [fact] No taste results yet (batch 1) "
            "^pending\n\n## Relations\n- supports [[Knowledge "
            "Base/Notes/Insights/rrf-fusion-beats-score-normalization]]"
        ),
    )
    due_state.reconcile(vault)
    due_state.reset_emission_state()
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "observe_memory")
    result = writer_lease.invoke_command(
        command,
        vault,
        path=ORIGIN,
        operation="add",
        category="result",
        content="Taste results: measured 2.",
        context="batch 1",
        id="result",
    )
    assert result.get("status") == "committed", result
    assert result["due_state"]["categories"]["transient_state_review"] == 1
    from exomem import vault as vault_module

    source = (vault / ORIGIN).read_text()
    state = semantic_contract.build_page_state(vault, ORIGIN, source)
    old = next(u for u in state.document.units if u.anchor == "pending")
    result = writer_lease.invoke_command(
        command,
        vault,
        path=ORIGIN,
        operation="update",
        unit_ref=old.unit_ref,
        expected_fingerprint=old.fingerprint,
        expected_hash=vault_module.content_hash(source),
        id="pending",
        category="fact",
        content="Previously no taste results existed.",
        context="batch 1",
    )
    assert result.get("status") == "committed", result
    assert (
        "due_state" not in result
        or "transient_state_review" not in result["due_state"]["categories"]
    )
    assert not [f for f in report(vault).findings if f.category == "transient_state_review"]


def test_visible_synthesis_support_need_not_copy_withheld_provenance(tmp_path, monkeypatch):
    from exomem.governance import egress

    for name in ("a", "b", "private"):
        write(tmp_path, name, f"Knowledge Base/Sources/{name}.md", "source")
    provenance = "\n".join(
        f"- relations: derived_from: [[{name}]]" for name in ("a", "b", "private")
    )
    text = "Taken together, the sources support cooling."
    write(tmp_path, f"## Finding\n- id: synthesis\n{provenance}\n\n{text}")
    destination = (
        "## Finding\n- id: copied\n"
        f"- relations: derived_from: [[{ORIGIN}#synthesis]]\n"
        "- relations: derived_from: [[a]]\n- relations: derived_from: [[b]]\n\n"
        f"{text}"
    )
    write(tmp_path, destination, DEST, "research-note")
    due_state.reconcile(tmp_path)
    assert due_state.served(tmp_path)
    monkeypatch.setattr(
        egress, "release_walk_filter", lambda *a, **k: lambda path: "private" not in path
    )
    assert due_state.served(tmp_path) is None


def test_private_candidates_do_not_consume_public_candidate_cap(tmp_path, monkeypatch):
    from exomem.governance import egress

    for name in ("a", "private"):
        write(tmp_path, name, f"Knowledge Base/Sources/{name}.md", "source")
    body = "\n".join(
        (
            f"## Finding\n- id: synthesis-{i}\n"
            "- relations: derived_from: [[a]]\n- relations: derived_from: [[private]]\n\n"
            f"Taken together, both sources support value {i}."
        )
        for i in range(12)
    )
    write(tmp_path, body + "\n" + method())
    due_state.reconcile(tmp_path)
    monkeypatch.setattr(
        egress, "release_walk_filter", lambda *a, **k: lambda path: "private" not in path
    )
    assert due_state.served(tmp_path)["categories"]["artifact_role_promotion"] == 1


def test_large_support_state_is_not_serialized_into_write_carrier_projection(tmp_path):
    write(tmp_path, method())
    for i in range(80):
        write(tmp_path, extracted(), f"Knowledge Base/Notes/Custom/support-{i}.md", "pattern")
    due_state.reconcile(tmp_path)
    before = due_state.state_path(tmp_path).stat().st_size
    assert before < 12_000
    write(tmp_path, method(cue="Reusable method: stir three times."))
    due_state.apply_write_delta(tmp_path, ORIGIN)
    assert due_state.state_path(tmp_path).stat().st_size < 12_000


def test_private_alias_does_not_make_public_source_ambiguous(tmp_path, monkeypatch):
    from exomem.governance import egress

    source_a, source_b = "Knowledge Base/Sources/a.md", "Knowledge Base/Sources/b.md"
    write(tmp_path, "Document A.", source_a, "source")
    write(tmp_path, "Document B.", source_b, "source")
    write(tmp_path, "Private alias.", "Knowledge Base/Sources/private/a.md", "source")
    write(
        tmp_path,
        (
            "## Finding\n- id: synthesis\n- relations: derived_from: [[a]]\n- "
            "relations: derived_from: [[b]]\n\nTaken together, both sources "
            "support cooling."
        ),
    )
    due_state.reconcile(tmp_path)
    monkeypatch.setattr(
        egress, "release_walk_filter", lambda *a, **k: lambda path: "/private/" not in path
    )
    assert due_state.served(tmp_path)["categories"]["artifact_role_promotion"] == 1
    assert len(report(tmp_path).findings) == 1


def test_compact_serving_does_not_enumerate_unbounded_origins(tmp_path, monkeypatch):
    from exomem import artifact_role_state as state

    write(tmp_path, method())
    index = report(tmp_path).role_state
    index["origins"].update({f"unknown-{i}": index["origins"][ORIGIN] for i in range(3000)})
    state.persist(tmp_path, index)

    def forbidden(*args, **kwargs):
        pytest.fail("unbounded origins reached the composer")

    monkeypatch.setattr(state, "compose", forbidden)
    assert state.served(tmp_path, lambda _p: True) == ([], dict.fromkeys(FAMILIES, "capped"))


def test_actual_persisted_delta_updates_only_bounded_origin_keys(tmp_path, monkeypatch):
    import sqlite3

    from exomem import artifact_role_state as state

    write(tmp_path, method())
    due_state.reconcile(tmp_path)
    index = report(tmp_path).role_state
    index["origins"].update({f"origin-{i}": index["origins"][ORIGIN] for i in range(5000)})
    index["reverse"][ORIGIN] = {f"origin-{i}": True for i in range(5000)}
    state.persist(tmp_path, index)
    visited = set()
    original = state._Rows.__getitem__

    def lookup(rows, key):
        if rows.section == "origins":
            visited.add(key)
            assert len(visited) <= 9
        return original(rows, key)

    monkeypatch.setattr(state._Rows, "__getitem__", lookup)
    due_state.apply_write_delta(tmp_path, ORIGIN)
    assert due_state.state_path(tmp_path).stat().st_size < 12_000
    with sqlite3.connect(state.store_path(tmp_path)) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='support_epoch'").fetchone()[0] == 1
        import json

        tail = conn.execute(
            "SELECT payload FROM rows WHERE section='origins' AND key='origin-999'"
        ).fetchone()[0]
        assert json.loads(tail)["epoch"] == 0


def test_private_origins_do_not_consume_visible_origin_limit(tmp_path):
    from exomem import artifact_role_state as state

    write(tmp_path, method())
    index = report(tmp_path).role_state
    index["origins"].update({f"A-private-{i}": index["origins"][ORIGIN] for i in range(80)})
    state.persist(tmp_path, index)
    rows, coverage = state.served(tmp_path, lambda path: not path.startswith("A-private"))
    assert len(rows) == 1
    assert coverage["artifact_role_promotion"] == "complete"


def test_persisted_alias_fanout_is_not_materialized_as_json(tmp_path, monkeypatch):
    from exomem import artifact_role_state as state

    write(tmp_path, method())
    due_state.reconcile(tmp_path)
    index = report(tmp_path).role_state
    index["aliases"]["trial"] = [ORIGIN, *(f"alias-{i}" for i in range(5000))]
    state.persist(tmp_path, index)
    original = state._Rows.__getitem__

    def lookup(rows, key):
        assert rows.section != "aliases", "write materialized a whole alias list"
        return original(rows, key)

    monkeypatch.setattr(state._Rows, "__getitem__", lookup)
    due_state.apply_write_delta(tmp_path, ORIGIN)
    assert due_state.served(tmp_path)["categories"]["artifact_role_promotion"] == 1


def test_stale_negative_destination_is_unknown_until_reconcile(tmp_path):
    write(tmp_path, method())
    write(tmp_path, extracted("Reusable method: different content."), DEST, "pattern")
    due_state.reconcile(tmp_path)
    assert due_state.served(tmp_path)
    write(tmp_path, extracted(), DEST, "pattern")
    assert due_state.served(tmp_path) is None
    due_state.reconcile(tmp_path)
    assert due_state.served(tmp_path) is None


def test_empty_projection_accepts_first_experiment_delta(tmp_path):
    (tmp_path / "Knowledge Base").mkdir()
    due_state.reconcile(tmp_path)
    assert not due_state.load(tmp_path)["role_state"]["has_origins"]
    write(tmp_path, method())
    due_state.apply_write_delta(tmp_path, ORIGIN)
    assert due_state.load(tmp_path)["role_state"]["has_origins"]
    assert due_state.served(tmp_path)["categories"]["artifact_role_promotion"] == 1


def test_maximum_ambiguous_page_respects_actual_delta_and_serve_budget(tmp_path):
    import time

    from test_artifact_role_review import crowded_methods

    from exomem import artifact_role_state as state

    write(tmp_path, method())
    due_state.reconcile(tmp_path)
    write(tmp_path, crowded_methods())
    start = time.monotonic()
    due_state.apply_write_delta(tmp_path, ORIGIN)
    delta_seconds = time.monotonic() - start
    start = time.monotonic()
    rows, coverage = state.served(tmp_path, lambda _p: True)
    serve_seconds = time.monotonic() - start
    assert delta_seconds < 0.5
    assert serve_seconds < 0.5
    assert not rows
    assert coverage == dict.fromkeys(FAMILIES, "complete")


def correction():
    return (
        "## Claim\n- id: corrected\n"
        f"- relations: supersedes: [[{ORIGIN}#pending]]\n\n"
        "Previously no taste results existed."
    )


def test_cross_page_supersession_settles_and_deletion_reopens(tmp_path):
    write(tmp_path, pending())
    due_state.reconcile(tmp_path)
    write(tmp_path, correction(), DEST, "pattern")
    due_state.apply_write_delta(tmp_path, DEST)
    assert not report(tmp_path).findings
    assert due_state.served(tmp_path) is None
    (tmp_path / DEST).unlink()
    find.clear_cache()
    due_state.apply_write_delta(tmp_path, DEST)
    assert due_state.served(tmp_path)["categories"]["transient_state_review"] == 1


def test_private_supersession_does_not_settle_public_pending(tmp_path, monkeypatch):
    from exomem.governance import egress

    write(tmp_path, pending())
    write(tmp_path, correction(), DEST, "pattern")
    due_state.reconcile(tmp_path)
    assert due_state.served(tmp_path) is None
    monkeypatch.setattr(egress, "release_walk_filter", lambda *a, **k: lambda path: path != DEST)
    assert due_state.served(tmp_path)["categories"]["transient_state_review"] == 1
    assert len(report(tmp_path).findings) == 1


def test_changed_superseder_is_unknown_until_delta_reopens(tmp_path):
    write(tmp_path, pending())
    write(tmp_path, correction(), DEST, "pattern")
    due_state.reconcile(tmp_path)
    assert due_state.served(tmp_path) is None
    write(tmp_path, "## Claim\n- id: corrected\n\nUnrelated claim.", DEST, "pattern")
    assert due_state.served(tmp_path) is None
    due_state.apply_write_delta(tmp_path, DEST)
    assert due_state.served(tmp_path)["categories"]["transient_state_review"] == 1


def test_detector_overrun_invalidates_actual_delta_and_served_coverage(tmp_path, monkeypatch):
    from exomem import artifact_role_review as sensor
    from exomem import artifact_role_state as state

    write(tmp_path, method())
    due_state.reconcile(tmp_path)
    clock = [0.0]
    original = sensor.detect

    def overrun(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += 1.0
        return result

    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sensor, "detect", overrun)
    assert state.served(tmp_path, lambda _p: True) == ([], dict.fromkeys(FAMILIES, "unknown"))
    due_state.apply_write_delta(tmp_path, ORIGIN)
    monkeypatch.setattr(sensor, "detect", original)
    assert state.served(tmp_path, lambda _p: True) == ([], dict.fromkeys(FAMILIES, "unknown"))


@pytest.mark.parametrize("status", ["superseded", "retracted"])
def test_inactive_cross_page_superseder_cannot_settle(tmp_path, status):
    write(tmp_path, pending())
    write(tmp_path, correction(), DEST, "pattern", status)
    assert len(report(tmp_path).findings) == 1


def test_authored_reverse_chronology_is_quiet_through_actual_delta(tmp_path):
    write(tmp_path, pending())
    due_state.reconcile(tmp_path)
    body = pending(context="batch 1 2026-09-12", result_context="batch 1 2026-09-01")
    body = body.replace(
        "- id: pending", f"- id: pending\n- relations: related: [[{ORIGIN}#result]]"
    )
    write(tmp_path, body)
    due_state.apply_write_delta(tmp_path, ORIGIN)
    assert not report(tmp_path).findings
    assert due_state.served(tmp_path) is None
