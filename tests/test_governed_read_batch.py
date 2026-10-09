"""Governed unit admission never replays authority from an unheld page."""
from pathlib import Path

import pytest
from test_governance_egress import OPEN_PATH, _external, write_rule, write_scope

from exomem.governance import egress
from exomem.governance.principal import request_scope


def test_warm_unit_decision_refuses_a_new_hard_link(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    units = [{"ref": OPEN_PATH, "text": "A useful fact.", "provenance": {"path": OPEN_PATH}}]
    with request_scope(_external()):
        assert egress.classify_units(vault, units) == [egress.UNIT_KEPT]
    alias = vault / "second-name.md"
    alias.hardlink_to(vault / OPEN_PATH)
    with request_scope(_external()), egress.disclosure_boundary(vault, "activate_context") as collector:
        assert egress.classify_units(vault, units) == [egress.UNIT_WITHHELD_SILENTLY]
        assert collector.outcomes
        assert all("content_hash" not in outcome.value and "size" not in outcome.value
                   for outcome in collector.outcomes)


@pytest.mark.parametrize("alias_kind", ["symlink", "unicode"])
def test_unit_reference_refuses_unsafe_or_ambiguous_leaf(vault: Path, alias_kind: str) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    target = vault / "Knowledge Base/Notes/café.md"
    if alias_kind == "symlink":
        target.symlink_to(vault / OPEN_PATH)
    else:
        target.write_text("# Public\n\nA fact.\n")
        target.with_name("cafe\u0301.md").write_text("# Different\n\nAnother fact.\n")
    relative = target.relative_to(vault).as_posix()
    units = [{"ref": relative, "text": "A useful fact.", "provenance": {"path": relative}}]
    with request_scope(_external()):
        assert egress.classify_units(vault, units) == [egress.UNIT_WITHHELD_SILENTLY]
        with egress.reader_view(vault).page_batch() as keep:
            assert keep(relative) is False


@pytest.mark.parametrize("change", ["leaf", "parent", "missing-parent"])
def test_changed_packet_mapping_discards_only_pending_claims(
    vault: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    from test_governance_egress import RESTRICTED_PATH

    from exomem import reserved_paths

    write_scope(vault)
    write_rule(vault, ceiling=0)
    units = [{"ref": OPEN_PATH, "text": "A useful fact.", "provenance": {"path": OPEN_PATH}}]
    missing = "Knowledge Base/new-parent/absent.md"
    units.append({"ref": missing, "text": "A missing page.", "provenance": {"path": missing}})
    units.append({"ref": RESTRICTED_PATH, "text": "A private fact.", "provenance": {"path": RESTRICTED_PATH}})
    validate = reserved_paths.GenericReadBatch.validate

    def exchange(batch):
        target = vault / OPEN_PATH
        if change == "leaf":
            replacement = target.with_name("replacement.md")
            replacement.write_bytes(target.read_bytes())
            replacement.replace(target)
        elif change == "parent":
            parent = target.parent
            parent.rename(parent.with_name("former-parent"))
            parent.mkdir()
            target.write_text("# Replacement\n")
        else:
            target = vault / missing
            target.parent.mkdir()
            target.write_text("# Now present\n")
        validate(batch)

    with request_scope(_external()), egress.disclosure_boundary(vault, "activate_context") as collector:
        egress.record_direct_text_release("Prior independent content.", stable_ref="earlier", representation="page_body")
        prior_outcomes = list(collector.outcomes)
        prior_claims = set(collector.path_outcomes)
        prior_memo = dict(egress._DECISION_MEMO)
        with monkeypatch.context() as changes:
            changes.setattr(reserved_paths.GenericReadBatch, "validate", exchange)
            assert egress.classify_units(vault, units) == [egress.UNIT_WITHHELD_SILENTLY] * len(units)
        assert collector.outcomes == prior_outcomes
        assert collector.path_outcomes == prior_claims
        assert dict(egress._DECISION_MEMO) == prior_memo
        assert egress.classify_units(vault, units)[0] == egress.UNIT_KEPT
        assert len(collector.outcomes) > len(prior_outcomes)


def test_repeated_held_read_uses_fresh_bytes(vault: Path) -> None:
    from exomem import reserved_paths

    with reserved_paths.generic_read_batch(vault, (OPEN_PATH,)) as batch:
        before = batch.read(OPEN_PATH).snapshot
        assert before is not None
        replacement = before.data + b"\nUpdated within the request.\n"
        (vault / OPEN_PATH).write_bytes(replacement)
        after = batch.read(OPEN_PATH).snapshot
        assert after is not None and after.data == replacement


def test_repeated_held_read_refuses_a_replaced_leaf(vault: Path) -> None:
    # Final validation sees only the last identity, so earlier bytes need this refusal.
    from exomem import reserved_paths

    with pytest.raises(reserved_paths.ReservedPathLeafError) as refused:
        with reserved_paths.generic_read_batch(vault, (OPEN_PATH,)) as batch:
            assert batch.read(OPEN_PATH).snapshot is not None
            target = vault / OPEN_PATH
            replacement = target.with_name("replacement.md")
            replacement.write_bytes(target.read_bytes())
            replacement.replace(target)
            batch.read(OPEN_PATH)
    assert refused.value.code == "IDENTITY_CHANGED"


@pytest.mark.parametrize("read_again", [False, True])
def test_changed_kb_namespace_discards_pending_reads(vault: Path, monkeypatch, read_again: bool) -> None:
    from exomem import reserved_paths

    path = "Brain/_Governance/control.md"
    target = vault / path
    target.parent.mkdir(parents=True)
    target.write_text("Ordinary content before the namespace changes.\n")
    published = []
    with pytest.raises(reserved_paths.ReservedPathLeafError):
        with reserved_paths.generic_read_batch(vault, (path,), publish=lambda: published.append(path)) as batch:
            assert batch.read(path).snapshot is not None
            monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Brain")
            if read_again:
                batch.read(path)
                pytest.fail("a changed namespace must refuse the next read")
    assert published == []


def test_warm_unit_refuses_a_published_private_identity(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import reserved_paths

    write_scope(vault)
    write_rule(vault, ceiling=0)
    units = [{"ref": OPEN_PATH, "text": "A useful fact.", "provenance": {"path": OPEN_PATH}}]
    with request_scope(_external()):
        assert egress.classify_units(vault, units) == [egress.UNIT_KEPT]
    identity = reserved_paths._lstat_identity(vault / OPEN_PATH)
    # Model a platform alias whose file identity belongs to a published private owner.
    published = reserved_paths.IdentityCatalogue(
        {(identity.device, identity.inode, identity.kind): "embeddings-store"}
    )
    monkeypatch.setattr(reserved_paths, "_published_identity_catalogue", lambda _root: published)
    with request_scope(_external()):
        assert egress.classify_units(vault, units) == [egress.UNIT_WITHHELD_SILENTLY]


@pytest.mark.parametrize("background", [False, True])
def test_unrelated_owner_publication_does_not_discard_an_admitted_unit(
    vault: Path, monkeypatch: pytest.MonkeyPatch, background: bool,
) -> None:
    import threading

    from exomem import reserved_paths

    write_scope(vault)
    write_rule(vault, ceiling=0)
    units = [{"ref": OPEN_PATH, "text": "A useful fact.", "provenance": {"path": OPEN_PATH}}]
    read = reserved_paths.GenericReadBatch.read
    published = False
    failures = []

    def publish():
        try:
            with reserved_paths._subsystem_authority_scope("lexstore"):
                with reserved_paths._identity_coordination_scope(vault):
                    reserved_paths._publish_owner_identities(vault, "lexical-store", {})
        except Exception as error:  # noqa: BLE001 - report thread failures in the calling test.
            failures.append(error)

    def read_during_publication(batch, path):
        nonlocal published
        observed = read(batch, path)
        if path == OPEN_PATH and not published:
            published = True
            if background:
                thread = threading.Thread(target=publish, daemon=True)
                thread.start()
                thread.join(timeout=5)
                assert not thread.is_alive(), "the read blocked an unrelated owner"
            else:
                publish()
        return observed

    monkeypatch.setattr(reserved_paths.GenericReadBatch, "read", read_during_publication)
    with reserved_paths._owner_authority_scope("govern_memory"), request_scope(_external()):
        assert egress.classify_units(vault, units) == [egress.UNIT_KEPT]
    assert published and not failures


def test_final_private_publication_discards_pending_decisions(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import reserved_paths

    write_scope(vault)
    write_rule(vault, ceiling=0)
    units = [{"ref": OPEN_PATH, "text": "A useful fact.", "provenance": {"path": OPEN_PATH}}]
    read = reserved_paths.GenericReadBatch.read
    catalogue = reserved_paths._published_identity_catalogue
    acquired = None

    def capture_read(batch, path):
        nonlocal acquired
        observed = read(batch, path)
        if path == OPEN_PATH and observed.snapshot is not None:
            acquired = observed.snapshot.identity
        return observed

    def publish_acquired_identity(root):
        if acquired is None:
            return catalogue(root)
        # A newly published platform alias must invalidate already acquired bytes.
        return reserved_paths.IdentityCatalogue(
            {(acquired.device, acquired.inode, acquired.kind): "embeddings-store"}
        )

    before = dict(egress._DECISION_MEMO)
    with monkeypatch.context() as changes:
        changes.setattr(reserved_paths.GenericReadBatch, "read", capture_read)
        changes.setattr(reserved_paths, "_published_identity_catalogue", publish_acquired_identity)
        with request_scope(_external()), egress.disclosure_boundary(vault, "activate_context") as collector:
            assert egress.classify_units(vault, units) == [egress.UNIT_WITHHELD_SILENTLY]
            assert not collector.outcomes and not collector.path_outcomes
    assert dict(egress._DECISION_MEMO) == before
    with request_scope(_external()):
        assert egress.classify_units(vault, units) == [egress.UNIT_KEPT]


def test_packet_receipt_hashes_the_acquired_bytes_without_rereading(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    from exomem import reserved_paths

    write_scope(vault)
    write_rule(vault, ceiling=0)
    target = vault / OPEN_PATH
    acquired = target.read_bytes()
    validate = reserved_paths.GenericReadBatch.validate

    def later_edit(batch):
        target.write_bytes(acquired + b"\nA later edit.\n")
        validate(batch)

    monkeypatch.setattr(reserved_paths.GenericReadBatch, "validate", later_edit)
    units = [{"ref": OPEN_PATH, "text": "A useful fact.", "provenance": {"path": OPEN_PATH}}]
    with request_scope(_external()), egress.disclosure_boundary(vault, "activate_context") as collector:
        guarded = egress.guard_working_set(vault, {"units": units}, egress.AnnotatedHits(hits=[], active=True))
        assert guarded is not None and guarded["units"]
        outcomes = [outcome.value for outcome in collector.outcomes]
        assert outcomes and all(outcome["content_hash"] == hashlib.sha256(acquired).hexdigest()
                                and outcome["size"] == len(acquired) for outcome in outcomes)


def test_generic_read_does_not_widen_an_existing_owner_boundary(vault: Path) -> None:
    from exomem import reserved_paths

    write_scope(vault)
    write_rule(vault, ceiling=0)
    units = [{"ref": OPEN_PATH, "text": "A useful fact.", "provenance": {"path": OPEN_PATH}}]
    with request_scope(_external()):
        assert egress.classify_units(vault, units) == [egress.UNIT_KEPT]
        with reserved_paths._owner_authority_scope("govern_memory"):
            with reserved_paths._identity_coordination_scope(vault):
                assert egress.classify_units(vault, units) == [egress.UNIT_WITHHELD_SILENTLY]
                assert not reserved_paths._identity_coordination_active(vault, "lexical-store")
                assert not reserved_paths.owner_authorized("lexical-store")


def test_private_publication_cannot_overtake_the_validated_receipt(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from exomem import reserved_paths

    write_scope(vault)
    write_rule(vault, ceiling=0)
    units = [{"ref": OPEN_PATH, "text": "A useful fact.", "provenance": {"path": OPEN_PATH}}]
    attempting = threading.Event()
    published = threading.Event()
    failures = []

    def publish():
        try:
            with reserved_paths._subsystem_authority_scope("lexstore"):
                attempting.set()
                with reserved_paths._identity_coordination_scope(vault):
                    reserved_paths._publish_owner_identities(vault, "lexical-store", {})
                    published.set()
        except Exception as error:  # noqa: BLE001 - return thread failures to the test.
            failures.append(error)

    outcome = egress._outcome_for_decision
    thread = threading.Thread(target=publish, daemon=True)

    def during_receipt(*args, **kwargs):
        thread.start()
        assert attempting.wait(timeout=5)
        assert not published.wait(timeout=0.1), "private authority changed before the claim committed"
        outcome(*args, **kwargs)

    monkeypatch.setattr(egress, "_outcome_for_decision", during_receipt)
    try:
        with request_scope(_external()), egress.disclosure_boundary(vault, "activate_context") as collector:
            guarded = egress.guard_working_set(vault, {"units": units}, egress.AnnotatedHits(hits=[], active=True))
            assert guarded is not None and guarded["units"]
            assert collector.outcomes
    finally:
        if thread.ident is not None:
            thread.join(timeout=5)
    assert published.is_set() and not failures


@pytest.mark.parametrize("authority", ["membership", "raw", "tombstone"])
def test_compatibility_alias_cannot_bypass_resource_authority(
    vault: Path, monkeypatch: pytest.MonkeyPatch, authority: str,
) -> None:
    from test_governance_egress import RESTRICTED_PATH, _hit

    from exomem.governance import lifecycle, raw_protection

    write_scope(vault)
    write_rule(vault, ceiling=0)
    path = RESTRICTED_PATH if authority == "membership" else OPEN_PATH
    if authority == "raw":
        target = (vault / OPEN_PATH).with_name(raw_protection.PREFIX + "private.md")
        target.write_text("# Private original\n\nPrivate content.\n")
        path = target.relative_to(vault).as_posix()
        alias = path.replace(raw_protection.PREFIX, raw_protection.PREFIX.replace("_", "＿"))
    else:
        if authority == "tombstone":
            monkeypatch.setattr(lifecycle, "tombstoned_paths", lambda _root: frozenset({path}))
        alias = path.replace("Notes", "Ｎotes")
    assert not (vault / alias).exists()
    with request_scope(_external()):
        for requested in (path, alias, alias, path):
            with egress.reader_view(vault).page_batch() as keep:
                assert not keep(requested)
            units = [{"ref": requested, "text": "Private content.", "provenance": {"path": requested}}]
            assert egress.annotate_hits(vault, [_hit(requested)]).hits == []
            assert egress.classify_units(vault, units) == [egress.UNIT_WITHHELD_SILENTLY]
            with egress.disclosure_boundary(vault, "activate_context") as collector:
                guarded = egress.guard_working_set(vault, {"units": units}, egress.AnnotatedHits(hits=[]))
            assert guarded is not None and guarded["units"] == []
            assert collector.outcomes
            assert all("content_hash" not in outcome.value for outcome in collector.outcomes)


def test_logical_unicode_read_keeps_physical_authority_and_receipt_identity(vault: Path) -> None:
    import hashlib
    import unicodedata

    from test_governance_egress import _hit

    logical = "Knowledge Base/Notes/café.md"
    physical = unicodedata.normalize("NFD", logical)
    content = "# Unicode source\n\nA useful fact.\n"
    (vault / physical).write_text(content)
    write_scope(vault, paths=physical)
    write_rule(vault, ceiling=0)
    units = [{"ref": logical, "text": "A useful fact.", "provenance": {"path": logical}}]
    with request_scope(_external()):
        assert egress.annotate_hits(vault, [_hit(logical)]).hits == []
        assert egress.classify_units(vault, units) == [egress.UNIT_WITHHELD_SILENTLY]
    from exomem.governance.principal import owner_principal

    with request_scope(owner_principal()), egress.disclosure_boundary(vault, "activate_context") as collector:
        assert egress.classify_units(vault, units) == [egress.UNIT_KEPT]
        guarded = egress.guard_working_set(vault, {"units": units}, egress.AnnotatedHits(hits=[]))
        assert guarded is not None and len(guarded["units"]) == 1
        # A scalar hit decision reads the spelling it is given, as the search index stores it.
        assert [hit.path for hit in egress.annotate_hits(vault, [_hit(physical)]).hits] == [physical]
    released = [outcome.value for outcome in collector.outcomes if "content_hash" in outcome.value]
    assert len(released) == 1
    assert released[0]["content_hash"] == hashlib.sha256(content.encode()).hexdigest()
    assert {identity for identity, outcome, _level in collector.path_outcomes if outcome == "released"} == {physical}


def test_withheld_alias_cannot_leave_canonical_graph_provenance(vault: Path) -> None:
    from test_governance_egress import RESTRICTED_PATH, _hit

    from exomem.find_types import GraphProvenance

    write_scope(vault)
    write_rule(vault, ceiling=0)
    alias = RESTRICTED_PATH.replace("Patterns", "Ｐatterns")
    expanded = _hit(OPEN_PATH)
    expanded.bm25_rank = None
    expanded.vector_score = None
    expanded.graph_hop = True
    expanded.graph_provenance = GraphProvenance(
        relation_type="supports", direction="outbound", seed=RESTRICTED_PATH,
    )
    with request_scope(_external()):
        release = egress.annotate_hits(vault, [_hit(alias), expanded])
    assert release.hits == []
    assert {alias, RESTRICTED_PATH} <= release.withheld_paths


def test_logical_markdown_reference_still_requires_parseable_acquired_bytes(vault: Path) -> None:
    from test_governance_egress import _hit

    write_scope(vault)
    write_rule(vault, ceiling=0)
    physical = "Knowledge Base/Notes/invalid.ｍｄ"
    logical = "Knowledge Base/Notes/invalid.md"
    (vault / physical).write_bytes(b"\xff")
    units = [{"ref": logical, "text": "Unverified content.", "provenance": {"path": logical}}]
    with request_scope(_external()):
        assert egress.annotate_hits(vault, [_hit(logical)]).hits == []
        assert egress.classify_units(vault, units) == [egress.UNIT_WITHHELD_SILENTLY]


def test_custom_reader_predicate_keeps_its_meaning_inside_a_page_batch(vault: Path) -> None:
    from test_governance_egress import RESTRICTED_PATH

    write_scope(vault)
    write_rule(vault, ceiling=0)
    expected = {OPEN_PATH: False, RESTRICTED_PATH: True, "missing.md": True}
    reader = egress.ReaderView(vault, expected.__getitem__, principal=_external(), purpose=None)
    with reader.page_batch() as keep:
        assert {path: keep(path) for path in expected} == expected
    assert {path: reader(path) for path in expected} == expected


def test_changed_neighbourhood_identity_discards_pending_claims_and_allows_retry(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    with request_scope(_external()), egress.disclosure_boundary(vault, "activate_context") as collector:
        egress.record_direct_text_release("Earlier independent content.", stable_ref="earlier", representation="page_body")
        prior_outcomes = list(collector.outcomes)
        prior_claims = set(collector.path_outcomes)
        prior_memo = dict(egress._DECISION_MEMO)
        reader = egress.reader_view(vault)
        with pytest.raises(egress.ReaderViewUnavailable), reader.page_batch() as keep:
            assert keep("Knowledge Base/Notes/absent.md")
            assert keep(OPEN_PATH)
            target = vault / OPEN_PATH
            replacement = target.with_name("replacement.md")
            replacement.write_bytes(target.read_bytes())
            replacement.replace(target)
        assert collector.outcomes == prior_outcomes
        assert collector.path_outcomes == prior_claims
        assert dict(egress._DECISION_MEMO) == prior_memo
        with reader.page_batch() as keep:
            assert keep(OPEN_PATH)
        assert collector.outcomes != prior_outcomes


@pytest.mark.parametrize("change", ["added", "removed", "unrelated"])
def test_neighbourhood_lifecycle_changes_validate_the_acquired_spelling(
    vault: Path, change: str,
) -> None:
    from contextlib import nullcontext

    from test_governance_egress import RESTRICTED_PATH

    from exomem.governance import lifecycle

    write_scope(vault, paths="Notes/**")
    write_rule(vault, ceiling=5)
    operation = None
    if change == "removed":
        operation = lifecycle.begin_deletion(vault, source_rel=OPEN_PATH, trash_rel="Knowledge Base/_trash/source.md")
    alias = OPEN_PATH.replace("Notes", "Ｎotes")
    try:
        with request_scope(_external()), egress.disclosure_boundary(vault, "activate_context") as collector:
            egress.record_direct_text_release("Earlier content.", stable_ref="earlier", representation="page_body")
            prior_outcomes = list(collector.outcomes)
            prior_memo = dict(egress._DECISION_MEMO)
            reader = egress.reader_view(vault)
            expected = pytest.raises(egress.ReaderViewUnavailable) if change != "unrelated" else nullcontext()
            with expected, reader.page_batch() as keep:
                assert keep(alias) is (change != "removed")
                if change == "removed":
                    lifecycle.abort_deletion(operation)
                else:
                    operation = lifecycle.begin_deletion(
                        vault, source_rel=RESTRICTED_PATH if change == "unrelated" else OPEN_PATH,
                        trash_rel="Knowledge Base/_trash/source.md",
                    )
            if change != "unrelated":
                assert collector.outcomes == prior_outcomes
                assert dict(egress._DECISION_MEMO) == prior_memo
            else:
                assert collector.outcomes != prior_outcomes
            with reader.page_batch() as keep:
                assert keep(alias) is (change != "added")
                assert keep("Knowledge Base/Notes/absent.md")
    finally:
        if operation is not None:
            lifecycle.abort_deletion(operation)


def test_terminal_packet_lifecycle_change_publishes_no_pending_claims(vault: Path, monkeypatch) -> None:
    from exomem.governance import lifecycle

    write_scope(vault, paths="Notes/**")
    write_rule(vault, ceiling=5)
    alias = OPEN_PATH.replace("Notes", "Ｎotes")
    units = [{"ref": alias, "text": "A useful fact.", "provenance": {"path": alias}}]
    decide = egress._decide_path
    operation = None

    def delete_after_decision(root, path, **kwargs):
        nonlocal operation
        decision = decide(root, path, **kwargs)
        if path == alias and operation is None:
            operation = lifecycle.begin_deletion(vault, source_rel=OPEN_PATH, trash_rel="Knowledge Base/_trash/source.md")
        return decision

    try:
        with request_scope(_external()), egress.disclosure_boundary(vault, "activate_context") as collector:
            egress.record_direct_text_release("Earlier content.", stable_ref="earlier", representation="page_body")
            prior_outcomes = list(collector.outcomes)
            prior_claims = set(collector.path_outcomes)
            prior_memo = dict(egress._DECISION_MEMO)
            with monkeypatch.context() as changes:
                changes.setattr(egress, "_decide_path", delete_after_decision)
                with pytest.raises(egress.ReaderViewUnavailable):
                    egress.guard_working_set(vault, {"units": units}, egress.AnnotatedHits(hits=[]))
            assert collector.outcomes == prior_outcomes
            assert collector.path_outcomes == prior_claims
            assert dict(egress._DECISION_MEMO) == prior_memo
            assert egress.classify_units(vault, units) == [egress.UNIT_WITHHELD_SILENTLY]
    finally:
        if operation is not None:
            lifecycle.abort_deletion(operation)


def test_ungoverned_page_admission_rechecks_a_new_lifecycle_floor(vault: Path) -> None:
    from exomem.governance import lifecycle

    operation = None
    try:
        with request_scope(_external()):
            reader = egress.reader_view(vault)
            with pytest.raises(egress.ReaderViewUnavailable), reader.page_batch() as keep:
                assert keep(OPEN_PATH)
                write_scope(vault, paths="Notes/**")
                write_rule(vault, ceiling=5)
                operation = lifecycle.begin_deletion(vault, source_rel=OPEN_PATH, trash_rel="Knowledge Base/_trash/source.md")
            with reader.page_batch() as keep:
                assert not keep(OPEN_PATH)
    finally:
        if operation is not None:
            lifecycle.abort_deletion(operation)
