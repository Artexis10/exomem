"""The batch boundary refuses authority failures before publishing any member."""

from types import SimpleNamespace

import pytest

from exomem import vault, vocabulary_authority, vocabulary_gate
from exomem.governance.principal import library_scope, request_scope


def test_activated_generic_batch_cannot_bypass_gate_without_operation(tmp_path, monkeypatch):
    class ActiveAuthority:
        def runtime_status(self):
            return SimpleNamespace(mode="v2")

    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda root: ActiveAuthority())
    first = tmp_path / "Knowledge Base/Entities/Organizations/group.md"
    second = tmp_path / "Knowledge Base/Notes/source.md"
    with library_scope(), pytest.raises(
        vocabulary_authority.VocabularyAuthorityDenied,
        match="canonical operation",
    ):
        vault.batch_atomic_write(
            [
                vault.PlannedWrite(first, "---\ntype: entity\n---\nA group.\n", create_only=True),
                vault.PlannedWrite(second, "---\ntype: insight\n---\nContext.\n", create_only=True),
            ],
            vault_root=tmp_path,
            post_commit_fanout=False,
        )
    assert not first.exists()
    assert not second.exists()


def test_unavailable_activation_refuses_before_any_canonical_replace(tmp_path, monkeypatch):
    class UnavailableAuthority:
        def runtime_status(self):
            raise vocabulary_authority.VocabularyAuthorityUnavailable("missing authority database")

    monkeypatch.setattr(
        vocabulary_authority, "VocabularyAuthority", lambda root: UnavailableAuthority()
    )
    target = tmp_path / "Knowledge Base/Notes/context.md"
    with library_scope(), pytest.raises(vocabulary_authority.VocabularyAuthorityUnavailable):
        vault.batch_atomic_write(
            [vault.PlannedWrite(target, "---\ntype: insight\n---\nContext.\n", create_only=True)],
            vault_root=tmp_path,
            post_commit_fanout=False,
        )
    assert not target.exists()


def test_inactive_batch_preserves_existing_write_behavior(tmp_path, monkeypatch):
    class InactiveAuthority:
        def runtime_status(self):
            return SimpleNamespace(mode="v1")

    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda root: InactiveAuthority())
    target = tmp_path / "Knowledge Base/Notes/context.md"
    content = "---\ntype: insight\n---\nContext.\n"
    with library_scope():
        vault.batch_atomic_write(
            [vault.PlannedWrite(target, content, create_only=True)],
            vault_root=tmp_path,
            post_commit_fanout=False,
        )
    assert target.read_text() == content


def test_operation_identity_does_not_escape_context(tmp_path):
    from exomem.governance.principal import owner_principal

    assert vocabulary_gate._OPERATION.get() is None
    with vocabulary_gate.operation_context(
        tmp_path, idempotency_key="private-internal-key", command_digest="a" * 64,
        receipt_id="canonical-receipt", principal=owner_principal(),
    ) as operation:
        assert vocabulary_gate._OPERATION.get() is operation
        assert "private-internal-key" not in repr(operation)
    assert vocabulary_gate._OPERATION.get() is None


@pytest.mark.parametrize("value", [None, "legacy-output", 42, ["output"]])
def test_legacy_nonterminal_output_is_unchanged(value):
    assert vocabulary_gate.attach_evidence(value, None) is value


@pytest.mark.parametrize("failure", [None, "evidence-close", "postcommit-log"])
def test_real_registry_batch_waits_for_exact_approval_and_publishes_receipt(tmp_path, monkeypatch, failure):
    from test_vocabulary_authority import (
        _activate,
        _approve_request,
        _principal,
        _store,
        install_unit_session_boundary,
    )

    from exomem import reserved_paths
    from exomem.governance.principal import request_scope

    install_unit_session_boundary(monkeypatch)
    root = tmp_path / "vault"
    target = root / "Knowledge Base/_Schema/entity-types.yaml"
    target.parent.mkdir(parents=True)
    before = "schema_version: 1\nentity_types: {}\n"
    after = (
        "schema_version: 1\nentity_types:\n  guild:\n"
        "    folder: Guilds\n    label: Guild\n    aliases: []\n"
        "    cue_nouns: []\n    capture_guidance: A durable group.\n"
    )
    target.write_text(before)
    authority = _store(root)
    principal = _principal()
    _activate(authority, principal)
    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda _: authority)

    def write():
        with request_scope(principal), reserved_paths._owner_authority_scope("schema_memory"):
            with vocabulary_gate.operation_context(
                root, idempotency_key="registry-one", command_digest="a" * 64,
                receipt_id="receipt-one", principal=principal,
            ) as context:
                vault.batch_atomic_write(
                    [vault.PlannedWrite(target, after)], vault_root=root,
                    post_commit_fanout=False,
                )
                return vocabulary_gate.attach_evidence({"state": "committed"}, context)

    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied) as denied:
        write()
    assert target.read_text() == before
    request_id = denied.value.details["vocabulary_request_id"]
    _approve_request(authority, principal, request_id)
    if failure == "evidence-close":
        from exomem import vocabulary_gate_evidence

        def close(_self):
            raise OSError("simulated read snapshot close failure")

        monkeypatch.setattr(vocabulary_gate_evidence.EvidenceBinding, "close", close)
    if failure == "postcommit-log":
        from exomem import writer_lease
        original = writer_lease.log_active_mutation_phase

        def log(phase, **kwargs):
            if phase == "canonical_files_committed":
                raise OSError("simulated postcommit log failure")
            return original(phase, **kwargs)

        monkeypatch.setattr(writer_lease, "log_active_mutation_phase", log)
        with pytest.raises(OSError, match="postcommit log"):
            write()
        assert target.read_text() == after
        return
    terminal = write()
    assert target.read_text() == after
    use = terminal["additive_authority"]["uses"][0]
    assert use["authorities"][0]["action"] == "entity_type.add"
    assert authority.request_status(request_id, principal=principal).state == "approved"


def test_v2_link_creation_with_connection_requires_and_uses_each_effect_authority(
    vault, warm_managed_cell, monkeypatch,
):
    """The real entity writer seals its index, log, and review sidecars."""
    import datetime as dt
    import uuid

    from test_vocabulary_authority import (
        _activate,
        _approve_request,
        _grant,
        _principal,
        _store,
        install_unit_session_boundary,
    )

    from exomem import activation_manifest, epistemic_graph, find, freshness, link, memory_refs

    install_unit_session_boundary(monkeypatch)
    target = vault / "Knowledge Base/Entities/Decisions/Established.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: entity\nentity_type: decision\nstatus: active\n"
        f"exomem_id: {uuid.uuid4()}\ntitle: Established\nproject: project-alpha\n"
        "decision_status: accepted\n---\n# Established\n",
        encoding="utf-8",
    )
    keys = vault / "Knowledge Base/_Schema/project-keys.yaml"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text("projects:\n  project-alpha: Project Alpha\n", encoding="utf-8")
    with library_scope():
        activation_manifest.ensure_manifest(vault)
    warm_managed_cell(vault)
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    checkpoint = freshness.live_recall_checkpoint(vault, "vault")
    assert checkpoint is not None
    assert find.recall_resolver_snapshot(
        vault, allow_fallback=False, expected_checkpoint=checkpoint,
    ) is not None
    assert memory_refs.ReferenceIndex(vault).available()

    authority = _store(vault)
    principal = _principal()
    _activate(authority, principal)
    _grant(
        authority, principal, actions=("entity.create",),
        scope=vocabulary_authority.AuthorityScope.vault_wide(), expires_at=1_700_000_300,
    )
    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda _: authority)

    def write():
        with request_scope(principal), library_scope():
            with vocabulary_gate.operation_context(
                vault, idempotency_key="link-with-connection", command_digest="b" * 64,
                receipt_id="link-receipt", principal=principal,
            ):
                return link.link(
                    vault, entity_type="decision", name="Bounded change", summary="An approved change.",
                    project="project-alpha", decision_status="accepted",
                    relations=[{"kind": "depends_on", "target": "Entities/Decisions/Established"}],
                    today=dt.date(2026, 9, 7),
                )

    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied) as denied:
        write()
    request_id = denied.value.details["vocabulary_request_id"]
    operation = authority.inspect_request_for_owner(request_id, principal=principal)
    assert {effect.action for effect in operation.effects} == {"entity.create", "edge.add"}
    _approve_request(authority, principal, request_id)

    result = write()
    assert (vault / result.path).is_file()


def test_v2_existing_entity_edit_reaches_existing_owner_after_derived_outputs(
    vault, warm_managed_cell, monkeypatch,
):
    import datetime as dt
    import uuid

    from test_vocabulary_authority import (
        _activate,
        _principal,
        _store,
        install_unit_session_boundary,
    )

    from exomem import activation_manifest, edit, epistemic_graph, find, freshness

    install_unit_session_boundary(monkeypatch)
    path = vault / "Knowledge Base/Entities/People/Hydrated.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: entity\nentity_type: person\nstatus: active\n"
        f"exomem_id: {uuid.uuid4()}\ntitle: Hydrated\n---\n# Hydrated\n\nOriginal body.\n",
        encoding="utf-8",
    )
    with library_scope():
        activation_manifest.ensure_manifest(vault)
    warm_managed_cell(vault)
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    checkpoint = freshness.live_recall_checkpoint(vault, "vault")
    assert checkpoint is not None
    find.recall_resolver_snapshot(vault, allow_fallback=False, expected_checkpoint=checkpoint)

    authority = _store(vault)
    principal = _principal()
    _activate(authority, principal)
    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda _: authority)
    with request_scope(principal), library_scope():
        with vocabulary_gate.operation_context(
            vault, idempotency_key="hydrate-existing", command_digest="c" * 64,
            receipt_id="hydrate-receipt", principal=principal,
        ):
            result = edit.edit(
                vault, path="Knowledge Base/Entities/People/Hydrated.md", why="hydrate evidence",
                old_string="Original", new_string="Hydrated", today=dt.date(2026, 9, 7),
            )

    assert result.semantic["path"] == "Knowledge Base/Entities/People/Hydrated.md"
    assert "Hydrated body." in path.read_text(encoding="utf-8")


def test_v2_mixed_registry_addition_and_existing_definition_change_refuses_atomically(
    tmp_path, monkeypatch,
):
    from test_vocabulary_authority import (
        _activate,
        _grant,
        _principal,
        _store,
        install_unit_session_boundary,
    )

    install_unit_session_boundary(monkeypatch)
    root = tmp_path / "vault"
    target = root / "Knowledge Base/_Schema/entity-types.yaml"
    target.parent.mkdir(parents=True)
    before = (
        "schema_version: 1\nentity_types:\n  guild:\n"
        "    folder: Guilds\n    label: Guild\n    aliases: []\n"
        "    cue_nouns: []\n    capture_guidance: A durable group.\n"
    )
    after = (
        "schema_version: 1\nentity_types:\n  guild:\n"
        "    folder: Guilds\n    label: Changed guild\n    aliases: []\n"
        "    cue_nouns: []\n    capture_guidance: A durable group.\n"
        "  club:\n    folder: Clubs\n    label: Club\n    aliases: []\n"
        "    cue_nouns: []\n    capture_guidance: A durable club.\n"
    )
    target.write_text(before, encoding="utf-8")
    authority = _store(root)
    principal = _principal()
    _activate(authority, principal)
    _grant(
        authority, principal, actions=("entity_type.add",),
        scope=vocabulary_authority.AuthorityScope.vault_wide(), expires_at=1_700_000_300,
    )
    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda _: authority)

    with request_scope(principal), library_scope(), vocabulary_gate.operation_context(
        root, idempotency_key="mixed-registry", command_digest="d" * 64,
        receipt_id="mixed-receipt", principal=principal,
    ), pytest.raises(
        vocabulary_authority.VocabularyAuthorityDenied,
        match="mixed structural effects require existing owner confirmation",
    ):
        vault.batch_atomic_write([vault.PlannedWrite(target, after)], vault_root=root, post_commit_fanout=False)
    assert target.read_text(encoding="utf-8") == before


def _activated_root(tmp_path, monkeypatch):
    from test_vocabulary_authority import (
        _activate,
        _principal,
        _store,
        install_unit_session_boundary,
    )

    install_unit_session_boundary(monkeypatch)
    root = tmp_path / "vault"
    root.mkdir()
    authority = _store(root)
    _activate(authority, _principal())
    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda _: authority)
    return root


def _prepared(data: bytes) -> vault.PreparedBinaryContent:
    import hashlib
    import io

    return vault.PreparedBinaryContent(io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest())


def test_v2_source_original_whose_bytes_changed_after_its_declaration_is_refused(tmp_path, monkeypatch):
    """The original is opaque, so its declared digest is the only thing that binds it to its Source page."""
    from exomem import vocabulary_auxiliaries

    root = _activated_root(tmp_path, monkeypatch)
    page = vault.PlannedWrite(
        root / "Knowledge Base/Sources/Sessions/notes.md.txt.md", "---\ntype: source\n---\n# Notes\n",
        create_only=True, expected_hash=vault.MISSING_CONTENT_HASH,
    )
    original = vault.PlannedWrite(
        root / "Knowledge Base/Sources/Sessions/notes.md.txt", _prepared(b"# Notes as uploaded\n"),
        create_only=True, expected_hash=vault.MISSING_CONTENT_HASH,
    )
    bound = vocabulary_auxiliaries.seal(
        root, primary=page, derived=[(vocabulary_auxiliaries.SOURCE_ORIGINAL, original)],
    )
    original.content = _prepared(b"# Other notes swapped in before the commit\n")

    with library_scope(), pytest.raises(
        vocabulary_authority.VocabularyAuthorityDenied, match="declared Source original changed",
    ):
        vault.batch_atomic_write(
            [page, original], vault_root=root, post_commit_fanout=False, _vocabulary_auxiliaries=bound,
        )
    assert not page.path.exists()
    assert not original.path.exists()


def test_v2_generic_write_of_a_non_markdown_file_without_a_source_page_is_refused(tmp_path, monkeypatch):
    root = _activated_root(tmp_path, monkeypatch)
    original = vault.PlannedWrite(
        root / "Knowledge Base/Sources/Sessions/notes.md.txt", _prepared(b"# Notes as uploaded\n"),
        create_only=True, expected_hash=vault.MISSING_CONTENT_HASH,
    )

    with library_scope(), pytest.raises(
        vocabulary_authority.VocabularyAuthorityDenied, match="current supported evidence",
    ):
        vault.batch_atomic_write([original], vault_root=root, post_commit_fanout=False)
    assert not original.path.exists()



@pytest.mark.parametrize(
    "smuggled_name",
    [
        # A page declared as an original would skip classification, so its entity and links would land ungoverned.
        "smuggled.md",
        # Windows reads `smuggled.md.` as `smuggled.md`.
        "smuggled.md.",
        # Another Source's container custody is not this page's to write.
        "other.asice.documents/" + "ab" * 32 + ".pdf",
    ],
)
def test_v2_source_original_outside_its_source_is_refused(activated_vault, smuggled_name):
    import dataclasses

    from exomem import vocabulary_auxiliaries

    root = activated_vault
    page = vault.PlannedWrite(
        root / "Knowledge Base/Sources/Sessions/notes.md.txt.md", "---\ntype: source\n---\n# Notes\n",
        create_only=True, expected_hash=vault.MISSING_CONTENT_HASH,
    )
    smuggled = vault.PlannedWrite(
        root / "Knowledge Base/Sources/Sessions" / smuggled_name,
        _prepared(b"---\nentity_type: guild\n---\n# Smuggled\n\nSee [[Unrelated page]].\n"),
        create_only=True, expected_hash=vault.MISSING_CONTENT_HASH,
    )
    with pytest.raises(ValueError, match="VOCABULARY_AUXILIARY_PATH_INVALID"):
        vocabulary_auxiliaries.seal(root, primary=page, derived=[(vocabulary_auxiliaries.SOURCE_ORIGINAL, smuggled)])

    # A declaration that reaches the gate without `seal`'s check must meet the same bound there.
    forged = dataclasses.replace(
        vocabulary_auxiliaries.seal(root, primary=page, derived=[]),
        entries=(vocabulary_auxiliaries.DerivedAuxiliary(
            vocabulary_auxiliaries.SOURCE_ORIGINAL, f"Knowledge Base/Sources/Sessions/{smuggled_name}",
            None, smuggled.content.sha256,
        ),),
    )
    with library_scope(), pytest.raises(
        vocabulary_authority.VocabularyAuthorityDenied, match="declared Source original is outside its Source",
    ):
        vault.batch_atomic_write(
            [page, smuggled], vault_root=root, post_commit_fanout=False, _vocabulary_auxiliaries=forged,
        )
    assert not page.path.exists()
    assert not smuggled.path.exists()


def test_v2_capture_that_links_to_a_missing_page_is_refused_cleanly(activated_vault):
    """Reading the identity of a page that does not exist crashed the classifier instead of refusing the link."""
    from exomem import add
    from exomem import schema as schema_module

    with library_scope(), pytest.raises(
        vocabulary_authority.VocabularyAuthorityDenied, match="current supported evidence",
    ):
        add.add(
            activated_vault, schema_module.load_source_schema(activated_vault),
            content="See [[Unrelated page]] for the rest.", title="Linked notes", source_type="session",
        )
    assert not list((activated_vault / "Knowledge Base/Sources/Sessions").glob("*linked-notes*"))


def test_v2_new_page_without_an_identity_that_links_to_an_existing_page_is_refused_cleanly(activated_vault):
    """The classifier read the new page's identity from disk before the write had created it."""
    page = vault.PlannedWrite(
        activated_vault / "Knowledge Base/Notes/linked.md",
        "---\ntitle: Linked\n---\n# Linked\n\nSee [[Ada Lovelace]].\n",
        create_only=True, expected_hash=vault.MISSING_CONTENT_HASH,
    )
    with library_scope(), pytest.raises(
        vocabulary_authority.VocabularyAuthorityDenied, match="current supported evidence",
    ):
        vault.batch_atomic_write([page], vault_root=activated_vault, post_commit_fanout=False)
    assert not page.path.exists()


def test_v2_internal_write_of_a_page_into_the_episode_journal_folder_is_classified(activated_vault):
    """The episode journal folder passes the gate only for the store's JSON, so a page there keeps its links governed."""
    from exomem import episode_store

    page = vault.PlannedWrite(
        episode_store.EpisodeStore(activated_vault).root / "smuggled.md",
        "---\ntitle: Smuggled\n---\n# Smuggled\n\nSee [[Unrelated page]].\n",
        create_only=True, expected_hash=vault.MISSING_CONTENT_HASH,
    )
    with library_scope(), pytest.raises(
        vocabulary_authority.VocabularyAuthorityDenied, match="current supported evidence",
    ):
        vault.batch_atomic_write([page], vault_root=activated_vault, post_commit_fanout=False)
    assert not page.path.exists()


def test_v2_refuses_an_undeclared_write_above_the_image_bound_without_reading_it(tmp_path, monkeypatch):
    """Reading an oversized image into memory only to refuse it is the allocation the byte bound exists to prevent."""
    import os

    from exomem import vocabulary_effects

    class CountingOS:
        """`os` as the gate sees it, counting the bytes that the gate reads."""

        read_bytes = 0

        def read(self, descriptor, count):
            chunk = os.read(descriptor, count)
            self.read_bytes += len(chunk)
            return chunk

        def __getattr__(self, name):
            return getattr(os, name)

    root = _activated_root(tmp_path, monkeypatch)
    counting = CountingOS()
    monkeypatch.setattr(vocabulary_gate, "os", counting)
    blob = vault.PlannedWrite(
        root / "Knowledge Base/Evidence/blob.bin", _prepared(b"\0" * (vocabulary_effects.MAX_IMAGE_BYTES + 1)),
        create_only=True, expected_hash=vault.MISSING_CONTENT_HASH,
    )

    with library_scope(), pytest.raises(vocabulary_authority.VocabularyAuthorityDenied, match="byte bound"):
        vault.batch_atomic_write([blob], vault_root=root, post_commit_fanout=False)
    assert counting.read_bytes == 0
    assert not blob.path.exists()
