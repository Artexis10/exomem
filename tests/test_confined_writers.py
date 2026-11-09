"""A restricted writer writes only inside its writable scopes and its inbox.

A restricted writer is a non-owner under a governed vault, or the owner reached through
a connector client that denies a scope. Its client entry in the host configuration lists
the scopes it may write (`writable_scope_ids`); a configured capture path, selected by an
inbox scope, is its private inbox. Every other target answers
`WRITE_REFUSED: target is unavailable` whether the target is withheld, absent or only
read-only, and the owner's own writes stay as they were.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from test_connector_boundary import (
    INBOX,
    PRIVATE,
    PUBLIC,
    SCOPE,
    configured_boundary as configured_boundary,
)

from granted_writer import grant_writer

from exomem import commands, find, lexstore, state_migration, writer_lease
from exomem import schema as schema_module
from exomem.governance import connector_boundary, egress, membership, policy, principal

RESIDUAL = "01ARZ3NDEKTSV4RRFFQ69G5FAY"
OWN = "01ARZ3NDEKTSV4RRFFQ69G5FAZ"
SHARED = "01ARZ3NDEKTSV4RRFFQ69G5FB2"
OWN_PAGE = "Knowledge Base/Notes/own.md"
OWN_NOTE = "Knowledge Base/Notes/Insights/own-note.md"
SHARED_PAGE = "Knowledge Base/Notes/shared.md"
NEW_INBOX_PAGE = "Knowledge Base/Capture/new.md"
SHARED_SOURCES = "01ARZ3NDEKTSV4RRFFQ69G5FB5"
PRIVATE_SOURCES = "01ARZ3NDEKTSV4RRFFQ69G5FB6"
SOURCES_INBOX = "Knowledge Base/Sources/Inbox/writer"
NOTES_INBOX = "Knowledge Base/Notes/Inbox/writer"
INBOX_FOLDERS = (NOTES_INBOX, SOURCES_INBOX)
INBOX_GLOBS = ("Notes/Inbox/writer/**", "Sources/Inbox/writer/**")
NEW_OUTSIDE_PAGE = "Knowledge Base/Notes/created.md"
REFUSED = "WRITE_REFUSED: target is unavailable"
NON_OWNER = "principal:" + "ab" * 32
_COMMANDS = {command.name: command for command in commands.PRODUCT_COMMANDS}


@pytest.fixture
def unarmed(configured_boundary, vault):
    """The base vault plus a residual scope, an own matter, a shared matter and a sibling client.

    `writer` is fenced from the other matter and from unclassified pages, and may write its
    inbox and its own matter. `sibling` is fenced from the writer's scopes. `full` is unfenced.
    """
    config, authenticate = configured_boundary
    scopes = vault / "Knowledge Base/_Governance/scopes"
    (scopes / "public.yaml").unlink()  # the public page is unclassified here, so the residual scope holds it
    for name, document in {
        "residual.yaml": f"governance_version: 1\nid: {RESIDUAL}\nresidual: true\ndefault_deny: true\n",
        "own.yaml": f'governance_version: 1\nid: {OWN}\nprojects: [own-matter]\npaths: ["Notes/Insights/own-*"]\n',
        "shared.yaml": f"governance_version: 1\nid: {SHARED}\nprojects: [shared-project]\n",
    }.items():
        (scopes / name).write_text(document, encoding="utf-8")
    # A page with a stable identity is edited through the semantic writer, which keeps a review and a history
    # entry beside it. The matter selects the note by path, so those entries lie outside the matter's scope.
    (vault / OWN_NOTE).parent.mkdir(parents=True, exist_ok=True)
    (vault / OWN_NOTE).write_text(
        "---\ntitle: Own note\ntype: insight\nexomem_id: 5a1e0000-0000-4000-8000-0000000000a1\nstatus: active\n---\n"
        "# Own note\n\n- [decision] Keep own text bounded.\n\n## Relations\n- relates_to [[own-peer]]\n", encoding="utf-8")
    (vault / OWN_NOTE).with_name("own-peer.md").write_text(
        "---\ntype: insight\nstatus: active\n---\n# Own peer\n\n- [decision] Keep peer text short.\n", encoding="utf-8")
    for path, project, text in ((OWN_PAGE, "own-matter", "own text"), (SHARED_PAGE, "shared-project", "shared text"),
                                (PUBLIC, "public-project", "public information text"),
                                (PRIVATE, "private-project", "protected canary text")):
        (vault / path).write_text(f"---\nproject: {project}\n---\n{text}\n", encoding="utf-8")
    # One inbox per tree: a generated destination keeps its tree, so the recap and the note land
    # under Sources and Notes. The sorted-first capture path of a tree is the one the door picks.
    (scopes / "inbox.yaml").write_text(
        f"governance_version: 1\nid: {INBOX}\npaths: {json.dumps(['Capture/**', *INBOX_GLOBS])}\n", encoding="utf-8")
    data = json.loads(config.read_text())
    data["capture_paths"] = ["Knowledge Base/Capture", *INBOX_FOLDERS]
    fenced = [SCOPE, RESIDUAL]
    data["default_denied_scope_ids"] = [SCOPE, RESIDUAL, INBOX, OWN]
    data["clients"] = [
        {"issuer": data["clients"][0]["issuer"], "client_id": client, "denied_scope_ids": denied,
         "writable_scope_ids": writable}
        for client, denied, writable in (
            ("writer", fenced, [INBOX, OWN]),
            ("sibling", [*fenced, INBOX, OWN], []),
            ("full", [], []),
        )
    ]
    config.write_text(json.dumps(data))
    assert not policy.load(vault).blocked
    return config, authenticate


@pytest.fixture
def confined(unarmed, vault):
    """`unarmed`, with the connector boundary armed so the capture path is an inbox."""
    authority = state_migration.assert_offline_migration_authority(source="isolated confined writer stop window")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    return unarmed


def _writers(authenticate):
    """Each writer kind, as the principal that a real client binding resolves to.

    `fenced-connector` is the owner signed in through a fenced connector: audience `owner`, a client
    binding, a denied set and writable scopes. `bound-non-owner` has the same binding under another audience.
    """
    writer = authenticate("writer")
    return {
        "fenced-connector": writer,
        "bound-non-owner": dataclasses.replace(writer, audience_id=NON_OWNER),
        "unbound-non-owner": principal.RequestPrincipal(audience_id=NON_OWNER, surface="mcp"),
        "unfenced-connector": authenticate("full"),
        "administration": principal.owner_principal(surface="cli"),
    }


def _call(vault: Path, who, command: str, **kwargs: Any) -> Any:
    try:
        with principal.request_scope(who):
            result = writer_lease.invoke_command(_COMMANDS[command], vault, **kwargs)
    except Exception as error:  # noqa: BLE001 - the refusal text is the answer under test
        return {"__error__": type(error).__name__, "message": str(error)}
    return json.loads(json.dumps(result, sort_keys=True, default=str).replace(str(vault), "<vault>"))


def _refused(answer: Any) -> bool:
    return isinstance(answer, dict) and REFUSED in str(answer.get("message", ""))


def _edit(path: str) -> tuple[str, dict[str, Any]]:
    return "edit_memory", {"path": path, "why": "correct", "operation": {
        "kind": "replace_string", "old_string": "text", "new_string": "edited text"}}


def _create(path: str) -> tuple[str, dict[str, Any]]:
    return "manage_memory_file", {"operation": "create", "path": path, "content": "# New\n\nNew text.\n"}


# (target label, door request, whether each writer kind may write it)
_MATRIX = {
    "own matter page": (OWN_PAGE, _edit, dict(fenced=True, bound=True, unbound=False, unfenced=True, admin=True)),
    "own matter note with an identity": (OWN_NOTE, _edit, dict(fenced=True, bound=True, unbound=False, unfenced=True, admin=True)),
    "new inbox page": (NEW_INBOX_PAGE, _create, dict(fenced=True, bound=True, unbound=False, unfenced=True, admin=True)),
    "shared page it reads": (SHARED_PAGE, _edit, dict(fenced=False, bound=False, unbound=False, unfenced=True, admin=True)),
    "other matter page": (PRIVATE, _edit, dict(fenced=False, bound=False, unbound=False, unfenced=True, admin=True)),
    "unclassified page": (PUBLIC, _edit, dict(fenced=False, bound=False, unbound=False, unfenced=True, admin=True)),
    "new page outside the inbox": (NEW_OUTSIDE_PAGE, _create, dict(fenced=False, bound=False, unbound=False, unfenced=True, admin=True)),
}
_KINDS = {
    "fenced-connector": "fenced", "bound-non-owner": "bound", "unbound-non-owner": "unbound",
    "unfenced-connector": "unfenced", "administration": "admin",
}


@pytest.mark.parametrize("target", sorted(_MATRIX))
@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_a_writer_writes_only_the_targets_its_kind_may(confined, vault, kind, target):
    """The write is admitted exactly where the table says, and a refusal changes no byte."""
    _, authenticate = confined
    path, door, expected = _MATRIX[target]
    command, arguments = door(path)
    before = (vault / path).read_bytes() if (vault / path).exists() else None

    answer = _call(vault, _writers(authenticate)[kind], command, **arguments)

    after = (vault / path).read_bytes() if (vault / path).exists() else None
    if expected[_KINDS[kind]]:
        assert not _refused(answer), answer
        assert after is not None and after != before, answer
    else:
        assert _refused(answer), answer
        assert after == before


@pytest.mark.parametrize("path", [OWN_NOTE, NEW_INBOX_PAGE])
def test_a_fenced_writer_edits_and_appends_to_its_path_selected_page(confined, vault, path):
    """An auxiliary outside the page's path scope must not make its residual membership deny the edit."""
    _, authenticate = confined
    writer = authenticate("writer")
    if path == NEW_INBOX_PAGE:
        created = _call(vault, writer, *_create(path)[:1], **_create(path)[1])
        assert not _refused(created), created
    edited = _call(vault, writer, "edit_memory", path=path, why="Clarify the note.", operation={
        "kind": "replace_string", "old_string": "text" if path == OWN_NOTE else "New text.",
        "new_string": "Changed text.",
    })
    appended = _call(vault, writer, "manage_memory_file", operation="append", path=path, content="More text.")

    assert not _refused(edited), edited
    assert not _refused(appended), appended
    assert "Changed text." in (vault / path).read_text()
    assert "More text." in (vault / path).read_text()


@pytest.mark.parametrize("detail", ["compact", "full"])
def test_a_confined_observation_ack_and_exact_unit_read_keep_their_parent(confined, vault, detail):
    """A committed unit reference must survive terminal shaping and remain readable without revision history."""
    _, authenticate = confined
    writer = authenticate("writer")

    observed = _call(vault, writer, "observe_memory", path=OWN_NOTE, operation="add", category="finding",
                     content="Keep an independent observation.", response_detail=detail)

    assert isinstance(observed, dict) and observed.get("unit_ref"), observed
    if detail == "full":
        assert observed["diagnostics"]["operational"]["state"] == "committed", observed
    assert "Keep an independent observation." in (vault / OWN_NOTE).read_text()
    current = _call(vault, writer, "read_memory", path=OWN_NOTE, unit_ref=observed["unit_ref"])
    assert isinstance(current, dict) and current.get("status") == "found", current
    assert current["unit"]["unit_ref"] == observed["unit_ref"]
    assert current["unit"]["content"] == "Keep an independent observation."
    assert "revision_history" not in current
    removed = _call(vault, writer, "observe_memory", path=OWN_NOTE, operation="remove",
                    unit_ref=observed["unit_ref"], expected_hash=current["parent"]["content_hash"],
                    expected_fingerprint=current["unit"]["fingerprint"], response_detail=detail)
    assert isinstance(removed, dict) and "removed_unit_ref" in removed, removed
    if detail == "full":
        assert removed["diagnostics"]["operational"]["state"] == "committed", removed
    assert removed["removed_unit_ref"] == observed["unit_ref"]
    assert "Keep an independent observation." not in (vault / OWN_NOTE).read_text()


def _committed_unit_reply(path, source, reference):
    """The public committed-terminal protocol, without unrelated producer diagnostics."""
    from exomem import vault as vault_module

    return {"ok": True, "state": "committed", "status": "committed", "terminal": True, "mutated": True,
            "path": path, "after_hash": vault_module.content_hash(source), "unit_ref": reference}


@pytest.mark.parametrize("protection", ["connector", "raw"])
def test_a_valid_unit_parent_binding_keeps_connector_and_raw_floors(confined, vault, protection):
    from exomem import memory_refs

    _, authenticate = confined
    path = PRIVATE if protection == "connector" else f"{NOTES_INBOX}/__exomem_raw_v1__blocked.md"
    source = ("---\ntype: insight\nstatus: active\nproject: private-project\n"
              "exomem_id: 33333333-3333-4333-8333-333333333333\n---\n- [finding] Protected content. ^kept\n")
    if protection == "raw":
        source = source.replace("project: private-project\n", "")
    (vault / path).parent.mkdir(parents=True, exist_ok=True)
    (vault / path).write_text(source, encoding="utf-8", newline="\n")
    reply = _committed_unit_reply(path, source, f"{memory_refs.ref_from_markdown(source)}#kept")
    writer = dataclasses.replace(authenticate("writer"), audience_id=NON_OWNER)

    with principal.request_scope(writer):
        assert egress.postfilter("unit_mutation", reply, vault) is None


def test_a_sibling_unit_reference_cannot_inherit_a_permitted_parent_hash(confined, vault):
    _, authenticate = confined
    reply = _committed_unit_reply(OWN_NOTE, (vault / OWN_NOTE).read_bytes().decode("utf-8"),
                                  "exomem://memory/33333333-3333-4333-8333-333333333333#kept")
    (vault / PRIVATE).write_text(
        "---\nproject: private-project\nexomem_id: 33333333-3333-4333-8333-333333333333\n---\n"
        "- [finding] Protected sibling. ^kept\n", encoding="utf-8")

    with principal.request_scope(authenticate("writer")):
        assert egress.postfilter("unit_mutation", reply, vault) is None


@pytest.mark.parametrize("change", ["stale_hash", "changed_identity", "malformed_fragment"])
def test_unit_parent_binding_requires_current_hash_identity_and_fragment(confined, vault, change):
    from exomem import semantic_index
    from exomem import vault as vault_module

    _, authenticate = confined
    source = (vault / OWN_NOTE).read_bytes().decode("utf-8")
    reference = semantic_index.current_parent_index_state(vault, OWN_NOTE).document.units[0].unit_ref
    reply = _committed_unit_reply(OWN_NOTE, source, reference)
    with principal.request_scope(authenticate("writer")):
        current = commands.op_read_memory(vault, path=OWN_NOTE, unit_ref=reference)
        if change == "stale_hash":
            reply["after_hash"] = current["parent"]["content_hash"] = "0" * 64
        elif change == "changed_identity":
            source = source.replace("5a1e0000-0000-4000-8000-0000000000a1", "33333333-3333-4333-8333-333333333333")
            (vault / OWN_NOTE).write_text(source, encoding="utf-8", newline="")
            reply["after_hash"] = current["parent"]["content_hash"] = vault_module.content_hash(source)
        else:
            reply["unit_ref"] = current["unit_ref"] = reference.split("#", 1)[0] + "#bad_fragment!"
        assert egress.postfilter("unit_mutation", reply, vault) is None
        assert egress.postfilter("read_memory", current, vault) is None


@pytest.mark.parametrize("resolved", [True, False])
def test_a_non_owner_is_read_only_with_any_policy_and_no_host_config(vault, resolved):
    """Missing identity cannot turn a governed vault's read-only audience into an unrestricted writer."""
    scopes = vault / "Knowledge Base/_Governance/scopes"
    scopes.mkdir(parents=True, exist_ok=True)
    (scopes / "matter.yaml").write_text(
        f'governance_version: 1\nid: {OWN}\npaths: ["Notes/**"]\n', encoding="utf-8")
    who = principal.RequestPrincipal(audience_id=NON_OWNER, surface="mcp", resolved=resolved)

    answer = _call(vault, who, *_create(NEW_OUTSIDE_PAGE)[:1], **_create(NEW_OUTSIDE_PAGE)[1])

    assert _refused(answer), answer
    assert not (vault / NEW_OUTSIDE_PAGE).exists()


@pytest.mark.parametrize("purpose_source", ["explicit", "trusted-session"])
@pytest.mark.parametrize("level", [0, 6])
def test_a_confined_write_keeps_its_explicit_or_session_purpose(confined, vault, tmp_path, monkeypatch, purpose_source, level):
    """A host write grant must not override the audience's full-read ceiling for its effective purpose."""
    import time

    from exomem.governance import authorization_custody, authorization_session_authority, authorization_session_lifecycle
    from exomem.governance import store as authorization_store

    _, authenticate = confined
    writer = dataclasses.replace(authenticate("writer"), audience_id=NON_OWNER)
    purpose = "limited-purpose"
    rules = vault / "Knowledge Base/_Governance/rules"
    rules.mkdir(exist_ok=True)
    (rules / "purpose.yaml").write_text(
        f"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC1\nscope_ids: [{INBOX}]\n"
        f"audience: {NON_OWNER}\npurpose: {purpose}\nceiling: {level}\n", encoding="utf-8")
    if purpose_source == "explicit":
        writer = writer.with_purpose(purpose)
    else:
        from test_authorization_session_wire_reconnect import _configure_v4_authority

        _configure_v4_authority(vault, tmp_path / "purpose-custody", monkeypatch)
        now = int(time.time())
        custody = authorization_custody.load_authorization_custody(vault, now=now)
        connection = authorization_store.open_authorization_session_connection(vault)
        try:
            session = authorization_session_lifecycle.open_session(
                connection, custody=custody, principal_id=NON_OWNER, issuer_family="mcp-oauth",
                now=now, ttl_seconds=600)
            authorization_session_authority.declare_purpose(
                connection, context=session.context, audience=NON_OWNER, purpose=purpose,
                now=now, expires_at=now + 300)
        finally:
            connection.close()
        writer = writer.with_verified_authorization_session(session.context, issuer_family="mcp-oauth")
        assert writer.purpose is None
    with principal.request_scope(writer):
        ceiling = connector_boundary.write_ceiling(vault, writer)
        assert ceiling is not None and ceiling.purpose == purpose
        if purpose_source == "trusted-session" and level == 6:
            # This session fixture has an older projector namespace; prove its write admission without publishing content.
            connector_boundary.require_create(vault, NEW_INBOX_PAGE)
            assert not (vault / NEW_INBOX_PAGE).exists()
            return

    answer = _call(vault, writer, *_create(NEW_INBOX_PAGE)[:1], **_create(NEW_INBOX_PAGE)[1])

    if level == 0:
        assert _refused(answer), answer
        assert not (vault / NEW_INBOX_PAGE).exists()
    else:
        assert "__error__" not in answer, answer
        assert (vault / NEW_INBOX_PAGE).is_file()


def test_an_unavailable_purpose_refuses_only_a_restricted_ceiling(confined, vault, monkeypatch):
    """A failed trusted-purpose read cannot open a restricted target or burden an unfenced owner."""
    _, authenticate = confined
    writer = dataclasses.replace(authenticate("writer"), audience_id=NON_OWNER)

    def unavailable(*args, **kwargs):
        raise egress.AuthorizationSessionDecisionUnavailable()

    monkeypatch.setattr(egress, "_declared_purpose", unavailable)
    with principal.request_scope(writer), pytest.raises(ValueError, match=REFUSED):
        connector_boundary.require_create(vault, NEW_INBOX_PAGE)
    assert connector_boundary.write_ceiling(vault, authenticate("full")) is None
    assert connector_boundary.write_ceiling(vault, principal.owner_principal(surface="library")) is None
    assert not (vault / NEW_INBOX_PAGE).exists()


def test_an_empty_policy_does_not_resolve_a_write_purpose(vault, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("an empty-policy write must not read purpose state")

    monkeypatch.setattr(egress, "_declared_purpose", unexpected)
    assert connector_boundary.write_ceiling(vault, principal.most_restrictive_principal(surface="mcp")) is None


@pytest.mark.parametrize("path", [
    "Knowledge Base/_Governance/scopes/evil.yaml",
    "Knowledge Base/.exomem-activation.json",
    "Knowledge Base/_Governance/curation/../scopes/evil.yaml",
])
def test_a_vocabulary_grant_never_authorizes_policy_or_manifest_writes(confined, vault, monkeypatch, path):
    from exomem import activation_manifest

    _, authenticate = confined
    if path.endswith(".exomem-activation.json"):
        path = activation_manifest.manifest_path(vault).relative_to(vault).as_posix()
    # Even a registry-classified subject cannot turn a grant into policy or manifest authority.
    monkeypatch.setattr(connector_boundary, "_membership_sets", lambda *args, **kwargs: (frozenset(),))
    with principal.request_scope(authenticate("writer")):
        with pytest.raises(ValueError, match=REFUSED):
            connector_boundary.require_write(vault, path, content=b"{}", granted=True)


@pytest.mark.parametrize("component", ["", ".", ".."])
@pytest.mark.parametrize("folder", ["_Governance/curation", "_Schema/relation-reviews"])
def test_workflow_state_paths_reject_noncanonical_components(folder, component):
    assert not policy.is_workflow_state_path(f"Knowledge Base/{folder}/{component}/record.json")


def test_a_noncanonical_review_path_does_not_inherit_workflow_write_authority(confined, vault):
    _, authenticate = confined
    with principal.request_scope(authenticate("writer")):
        with pytest.raises(ValueError, match=REFUSED):
            connector_boundary.require_write(
                vault, "Knowledge Base/_Schema/relation-reviews/../SKILL.md", content=b"# Changed\n",
            )


def test_a_registry_history_seal_does_not_extend_a_grant_to_an_arbitrary_page(confined, vault):
    from exomem import vocabulary_auxiliaries
    from exomem import vault as vault_module

    _, authenticate = confined
    registry_path = "Knowledge Base/_Schema/entity-types.yaml"
    primary = vault_module.PlannedWrite(vault / registry_path, "schema_version: 1\n")
    forged = vault_module.PlannedWrite(
        vault / SHARED_PAGE, "# Changed\n",
        guard=vault_module.PathGuard.capture(
            vault, SHARED_PAGE, leaf_policy="content",
            expected_content_hash=vault_module.content_hash((vault / SHARED_PAGE).read_text()),
        ),
    )
    manifest = vocabulary_auxiliaries.seal(vault, primary=primary, derived=(("registry-history", forged),))
    with principal.request_scope(authenticate("writer")):
        with pytest.raises(ValueError, match="VOCABULARY_AUXILIARY"):
            vault_module._validate_batch_write_access(
                vault, (primary, forged), auxiliary_manifest=manifest, granted_paths=frozenset({registry_path}),
            )


@pytest.mark.parametrize("recursive", [False, True])
@pytest.mark.parametrize("inbox", [False, True])
def test_folder_delete_refusal_matches_write_admission(confined, vault, inbox, recursive):
    """A nonrecursive folder outside the inbox fails write admission; admitted and recursive folders stay protected."""
    _, authenticate = confined
    folder = "Knowledge Base/Capture/folder" if inbox else "Knowledge Base/Notes/folder"
    child = vault / folder / "page.md"
    child.parent.mkdir(parents=True)
    child.write_text("# Page\n\nVisible text.\n", encoding="utf-8")

    answer = _call(vault, authenticate("writer"), "manage_memory_file", operation="delete", path=folder,
                   confirm=True, recursive=recursive)

    expected = "AUDIENCE_RESTRICTED" if inbox or recursive else REFUSED
    assert expected in answer.get("message", ""), answer
    assert child.is_file()


@pytest.mark.parametrize("door", ["edit", "append", "observe", "replace", "move-source", "move-destination",
                                  "delete", "create-overwrite"])
def test_every_write_door_answers_a_read_only_page_as_a_hidden_or_absent_one(confined, vault, door):
    """One door per write kind: the page the writer reads but may not write, a withheld page and an absent one answer alike."""
    _, authenticate = confined
    writer = authenticate("writer")

    def request(path: str) -> tuple[str, dict[str, Any]]:
        return {
            "edit": _edit(path),
            "append": ("manage_memory_file", {"operation": "append", "path": path, "content": "More text."}),
            "observe": ("observe_memory", {"path": path, "operation": "add", "category": "finding", "content": "New."}),
            "replace": ("replace_memory", {"old_path": path, "content": "Replacement.\n", "title": "Replaced Page",
                                           "reason": "supersede"}),
            "move-source": ("manage_memory_file", {"operation": "move", "old_path": path,
                                                   "new_path": "Knowledge Base/Capture/moved.md"}),
            "move-destination": ("manage_memory_file", {"operation": "move", "old_path": OWN_PAGE, "new_path": path}),
            "delete": ("manage_memory_file", {"operation": "delete", "path": path, "confirm": True}),
            "create-overwrite": ("manage_memory_file", {"operation": "create", "path": path,
                                                        "content": "# New\n\nText.\n", "overwrite": True}),
        }[door]

    snapshot = {path: (vault / path).read_bytes() for path in (SHARED_PAGE, PRIVATE, OWN_PAGE)}
    answers = {
        label: _call(vault, writer, *request(path)[:1], **request(path)[1])
        for label, path in (("read-only", SHARED_PAGE), ("withheld", PRIVATE),
                            ("absent", "Knowledge Base/Notes/absent.md"))
    }

    assert all(_refused(answer) for answer in answers.values()), answers
    assert {path: (vault / path).read_bytes() for path in snapshot} == snapshot


def test_an_adoption_run_is_a_write_only_the_unrestricted_may_start(confined, vault):
    """`start` saves a run record in the Knowledge Base; a restricted writer's save is refused, so no run opens."""
    _, authenticate = confined
    runs = vault / "Knowledge Base/_Adoption/runs"

    refused = {
        kind: _call(vault, who, "adoption_studio", action="start", path="Knowledge Base/Notes")
        for kind, who in _writers(authenticate).items() if kind in ("fenced-connector", "bound-non-owner")
    }
    assert all(_refused(answer) for answer in refused.values()), refused
    assert not runs.exists()

    opened = _call(vault, principal.owner_principal(surface="cli"), "adoption_studio", action="start",
                   path="Knowledge Base/Notes")
    assert opened.get("run_id"), opened


def test_a_fenced_writer_cannot_declassify_its_own_page(confined, vault):
    """The page is writable before the write; bytes that would move it into another scope are refused."""
    _, authenticate = confined
    writer = authenticate("writer")
    before = (vault / OWN_PAGE).read_bytes()

    def patch(project: str) -> dict[str, Any]:
        return _call(vault, writer, "edit_memory", path=OWN_PAGE, why="reclassify",
                     operation={"kind": "patch_frontmatter", "field": "project", "value": project})

    into_other_matter = patch("private-project")
    out_of_matter = patch("shared-project")
    moved_out = _call(vault, writer, "manage_memory_file", operation="move", old_path=OWN_PAGE,
                      new_path=SHARED_PAGE.replace("shared", "moved"))

    assert all(_refused(answer) for answer in (into_other_matter, out_of_matter, moved_out)), (
        into_other_matter, out_of_matter, moved_out)
    assert (vault / OWN_PAGE).read_bytes() == before


def test_an_inbox_page_is_the_writers_and_the_owners_and_nobody_elses(confined, vault):
    """A writer's inbox page is named in its response; a sibling fenced connector never receives it."""
    _, authenticate = confined
    created = _call(vault, authenticate("writer"), *_create(NEW_INBOX_PAGE)[:1], **_create(NEW_INBOX_PAGE)[1])
    assert NEW_INBOX_PAGE in json.dumps(created)

    def visible_to(client: str) -> bool:
        with principal.request_scope(authenticate(client)):
            return egress.quick_page_visible(vault, NEW_INBOX_PAGE)

    assert [visible_to(client) for client in ("writer", "full", "sibling")] == [True, True, False]
    assert _refused(_call(vault, authenticate("sibling"), *_edit(NEW_INBOX_PAGE)[:1], **_edit(NEW_INBOX_PAGE)[1]))


def test_an_owner_page_keeps_exactly_the_membership_of_its_capture_path(confined, vault):
    """Even an unrestricted writer cannot put protected content into a capture path."""
    _, authenticate = confined
    page = "Knowledge Base/Capture/private.md"

    answer = _call(vault, authenticate("full"), "manage_memory_file", operation="create", path=page,
                   content="---\nproject: private-project\n---\nprivate text\n")

    assert _refused(answer), answer
    assert not (vault / page).exists()


def test_a_capture_page_has_a_membership_the_path_alone_gives(confined, vault):
    """The inbox is judged by its path: a page there is in the inbox scope and in no other."""
    assert membership.evaluate_path_selectors(vault, NEW_INBOX_PAGE, policy.load(vault)) == frozenset({INBOX})
    assert membership.evaluate_path_selectors(vault, PUBLIC, policy.load(vault)) == frozenset({RESIDUAL})


def test_a_vault_without_a_connector_configuration_keeps_every_callers_writes(vault, monkeypatch):
    """No policy and no connector configuration: a non-owner audience writes as before."""
    monkeypatch.delenv(connector_boundary.CONFIG_ENV, raising=False)
    stranger = principal.RequestPrincipal(audience_id=NON_OWNER, surface="mcp")
    page = vault / "Knowledge Base/Notes/open.md"
    page.write_text("---\nproject: open-project\n---\nopen text\n", encoding="utf-8")

    answer = _call(vault, stranger, *_edit("Knowledge Base/Notes/open.md")[:1], **_edit("Knowledge Base/Notes/open.md")[1])

    assert not _refused(answer), answer
    assert "edited text" in page.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The host configuration
# ---------------------------------------------------------------------------


def _ready(vault, config: Path, change) -> bool:
    data = json.loads(config.read_text())
    change(data)
    config.write_text(json.dumps(data))
    try:
        return connector_boundary.snapshot(vault) is not None
    except connector_boundary.BoundaryUnavailable:
        return False


def _client(data: dict, name: str) -> dict:
    return next(item for item in data["clients"] if item["client_id"] == name)


def _without_client(data: dict, name: str) -> None:
    data["clients"] = [item for item in data["clients"] if item["client_id"] != name]


def _writable_scope_the_client_denies(data: dict) -> None:
    _client(data, "writer")["writable_scope_ids"] = [SCOPE]


def _writable_scope_on_an_unfenced_client(data: dict) -> None:
    _without_client(data, "sibling")
    _client(data, "writer")["writable_scope_ids"] = []
    _client(data, "full")["writable_scope_ids"] = [INBOX]


def _writable_scope_readable_by_the_default_context(data: dict) -> None:
    _without_client(data, "sibling")
    data["default_denied_scope_ids"].remove(OWN)


def _writable_scope_readable_by_another_fenced_client(data: dict) -> None:
    _client(data, "sibling")["denied_scope_ids"].remove(OWN)


def _writable_scope_no_policy_defines(data: dict) -> None:
    _client(data, "writer")["writable_scope_ids"] = ["01ARZ3NDEKTSV4RRFFQ69G5FB3"]


def _writable_residual_scope(data: dict) -> None:
    _client(data, "writer").update(denied_scope_ids=[SCOPE], writable_scope_ids=[RESIDUAL])


def _residual_scope_missing_from_the_default_denied_set(data: dict) -> None:
    data["default_denied_scope_ids"].remove(RESIDUAL)
    for item in data["clients"]:
        if RESIDUAL in item["denied_scope_ids"]:
            item["denied_scope_ids"].remove(RESIDUAL)


@pytest.mark.parametrize("breach", [
    _writable_scope_the_client_denies,
    _writable_scope_on_an_unfenced_client,
    _writable_scope_readable_by_the_default_context,
    _writable_scope_readable_by_another_fenced_client,
    _writable_scope_no_policy_defines,
    _writable_residual_scope,
    _residual_scope_missing_from_the_default_denied_set,
], ids=lambda breach: breach.__name__.removeprefix("_"))
def test_the_host_refuses_a_configuration_that_breaks_a_writers_exclusivity(unarmed, vault, breach):
    """Each breach alone makes the boundary unavailable until the host repairs the file."""
    config, _ = unarmed
    valid = config.read_text()
    assert _ready(vault, config, lambda data: None)

    assert not _ready(vault, config, breach)

    config.write_text(valid)
    assert _ready(vault, config, lambda data: None)


def test_the_host_refuses_writable_scopes_in_a_vault_that_declares_no_residual_scope(unarmed, vault):
    """A writer's relation reviews name its inbox pages, so an open vault would serve them to everyone."""
    config, _ = unarmed
    scopes = vault / "Knowledge Base/_Governance/scopes"
    valid = config.read_text()
    residual_document = (scopes / "residual.yaml").read_text()

    def without_the_residual_scope(data: dict) -> None:
        data["default_denied_scope_ids"].remove(RESIDUAL)
        for item in data["clients"]:
            if RESIDUAL in item["denied_scope_ids"]:
                item["denied_scope_ids"].remove(RESIDUAL)

    def and_without_writable_scopes(data: dict) -> None:
        without_the_residual_scope(data)
        for item in data["clients"]:
            item["writable_scope_ids"] = []

    (scopes / "residual.yaml").unlink()
    assert not _ready(vault, config, without_the_residual_scope)
    config.write_text(valid)
    assert _ready(vault, config, and_without_writable_scopes), "only the writable scopes were refused"

    (scopes / "residual.yaml").write_text(residual_document)
    config.write_text(valid)
    assert _ready(vault, config, lambda data: None)


# ---------------------------------------------------------------------------
# Recaps
# ---------------------------------------------------------------------------

_OWNER_KEY = "ep-" + "a1" * 16
_WRITER_KEY = "ep-" + "b2" * 16


def _record_recap(vault: Path, who, key: str, subject: str) -> dict:
    with principal.request_scope(who):
        return writer_lease.invoke_command(
            _COMMANDS["episode_memory"], vault, schema_module.load_source_schema(vault),
            action="record", episode=key, subject=subject,
            summary=f"{subject}: what was settled.", worked_on=[f"Worked on {subject}"],
        )


def test_a_fenced_connectors_recap_lands_in_its_inbox_and_only_it_and_the_owner_receive_it(confined, vault):
    """The owner's recap stays in the shared folder and is closed to a fenced connector, as the writer's is to a sibling."""
    _, authenticate = confined
    owner_recap = _record_recap(vault, authenticate("full"), _OWNER_KEY, "Harbor lamp purchase")
    writer_recap = _record_recap(vault, authenticate("writer"), _WRITER_KEY, "Garden path layout")
    owner_path, writer_path = owner_recap["source"]["path"], writer_recap["source"]["path"]

    assert owner_path.startswith("Knowledge Base/Sources/Episodes/")
    assert writer_path.startswith(f"{SOURCES_INBOX}/Episodes/")

    def sees(client: str, path: str) -> bool:
        with principal.request_scope(authenticate(client)):
            return egress.quick_page_visible(vault, path)

    assert {client: (sees(client, owner_path), sees(client, writer_path)) for client in ("full", "writer", "sibling")} == {
        "full": (True, True), "writer": (False, True), "sibling": (False, False)}


@pytest.mark.usefixtures("anchors_packet")  # the rollback packet names each recap; maps is the default
def test_recent_context_serves_each_connector_only_the_recaps_it_may_read(confined, vault):
    from exomem import find, lexstore, working_set_index, working_set_runtime

    _, authenticate = confined
    _record_recap(vault, authenticate("full"), _OWNER_KEY, "Harbor lamp purchase")
    _record_recap(vault, authenticate("writer"), _WRITER_KEY, "Garden path layout")
    find.clear_cache()
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()

    def recaps(client: str) -> set[str]:
        with principal.request_scope(authenticate(client)):
            packet = commands.op_activate_context(vault, turn="What did we settle about the lamp and the garden path?")
        recent = packet["recent_context"]
        entries = recent["entries"] if isinstance(recent, dict) else recent
        return {entry["episode"] for entry in entries if entry.get("why") == "episode"}

    assert {client: recaps(client) for client in ("full", "writer", "sibling")} == {
        "full": {_OWNER_KEY, _WRITER_KEY}, "writer": {_WRITER_KEY}, "sibling": set()}


def test_a_recap_by_a_non_owner_with_no_client_entry_is_refused(confined, vault):
    _, authenticate = confined
    stranger = _writers(authenticate)["unbound-non-owner"]

    with pytest.raises(ValueError, match="WRITE_REFUSED"):
        _record_recap(vault, stranger, _WRITER_KEY, "Garden path layout")

    assert not (vault / SOURCES_INBOX).exists()


# ---------------------------------------------------------------------------
# Twin worlds
# ---------------------------------------------------------------------------


def _twin_vaults(unarmed, vault, tmp_path, monkeypatch) -> dict[str, Path]:
    """World A holds a hidden unclassified page and a hidden other-matter page; world B holds neither."""
    import shutil

    from exomem import find, lexstore, working_set_index, working_set_runtime

    inbox_page = vault / "Knowledge Base/Capture/mine.md"
    inbox_page.parent.mkdir(parents=True, exist_ok=True)
    inbox_page.write_text("---\nproject: public-project\n---\nmy own capture text\n", encoding="utf-8")
    twin = tmp_path / "twin"
    shutil.copytree(vault, twin)
    (twin / PUBLIC).unlink()
    (twin / PRIVATE).unlink()
    for root in (vault, twin):
        monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
        authority = state_migration.assert_offline_migration_authority(source="twin world stop window")
        state_migration.arm_connector_boundary_offline(root, authority=authority)
        find.clear_cache()
        lexstore.ensure_fresh(root)
        working_set_runtime.reset_caches_for_tests()
        working_set_index.WorkingSetIndex(root).rebuild()
    return {"A": vault, "B": twin}


def _twin_probes() -> dict[str, tuple[str, dict[str, Any]]]:
    return {
        "search-withheld": ("ask_memory", {"query": "protected canary"}),
        "search-unclassified": ("ask_memory", {"query": "public information"}),
        "search-own": ("ask_memory", {"query": "own text"}),
        "read-withheld": ("read_memory", {"path": PRIVATE}),
        "read-unclassified": ("read_memory", {"path": PUBLIC}),
        "browse-root": ("browse_memory", {"mode": "list", "path": ""}),
        "browse-overview": ("browse_memory", {"mode": "overview"}),
        "recent-context": ("activate_context", {"turn": "What did we settle about the protected canary and public information?"}),
        "edit-withheld": _edit(PRIVATE),
        "edit-unclassified": _edit(PUBLIC),
        "create-where-a-hidden-page-sits": _create(PUBLIC),
    }


@pytest.mark.parametrize("kind", ["fenced-connector", "bound-non-owner"])
def test_a_restricted_client_cannot_tell_a_hidden_page_from_an_absent_one(unarmed, vault, tmp_path, monkeypatch, kind):
    """Search, read, browse, recent context and every write door answer alike with and without the hidden pages."""
    _, authenticate = unarmed
    worlds = _twin_vaults(unarmed, vault, tmp_path, monkeypatch)
    answers: dict[str, dict[str, Any]] = {}
    for label, root in worlds.items():
        monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
        egress.clear_decision_memo()
        membership.clear_memo()
        who = _writers(authenticate)[kind]
        answers[label] = {probe: _call(root, who, command, **arguments)
                          for probe, (command, arguments) in _twin_probes().items()}

    identifiers = [answer["recent-context"]["served_context_id"] for answer in answers.values()]
    assert len(set(identifiers)) == 2, "composition identifiers are fresh for every response"
    for answer in answers.values():
        answer["recent-context"]["served_context_id"] = "<composition-id>"
    assert answers["A"] == answers["B"]
    # The twin test proves nothing if the client sees nothing: it still reads its own matter and its own capture.
    assert OWN_PAGE in json.dumps(answers["A"]["search-own"])
    assert OWN_PAGE in json.dumps(answers["A"]["browse-overview"])
    assert "Knowledge Base/Capture/mine.md" in json.dumps(answers["A"]["browse-overview"])
    assert all(_refused(answers["A"][probe]) for probe in ("edit-withheld", "edit-unclassified",
                                                          "create-where-a-hidden-page-sits"))


def test_a_fenced_recaps_unknown_and_withheld_about_refs_are_dropped_alike(confined, vault):
    """A fenced recorder cannot tell from its recap whether an `about` ref is absent or withheld from it."""
    from exomem import memory_refs

    _, authenticate = confined
    ids = {SHARED_PAGE: "11111111-1111-4111-8111-111111111111", PRIVATE: "22222222-2222-4222-8222-222222222222"}
    for path, memory_id in ids.items():
        page = vault / path
        page.write_text(page.read_text(encoding="utf-8").replace("---\n", f"---\nexomem_id: {memory_id}\n", 1),
                        encoding="utf-8")
    seen, withheld, absent = (memory_refs.memory_ref(memory_id)
                              for memory_id in (*ids.values(), "33333333-3333-4333-8333-333333333333"))

    def record(key: str, about: str) -> dict:
        with principal.request_scope(authenticate("writer")):
            return writer_lease.invoke_command(
                _COMMANDS["episode_memory"], vault, schema_module.load_source_schema(vault),
                action="record", episode=key, subject="Garden path layout", summary="What was settled.",
                worked_on=["Worked on the path"], about=[about],
            )

    kept, hidden, missing = (record("ep-" + digit * 16, ref) for digit, ref in
                             (("c3", seen), ("d4", withheld), ("e5", absent)))

    assert [result["about_skipped"] for result in (kept, hidden, missing)] == [0, 1, 1]
    assert {key: hidden[key] for key in ("about_skipped", "ledger", "idempotent")} == {
        key: missing[key] for key in ("about_skipped", "ledger", "idempotent")}


def test_an_unclassifiable_visible_label_refuses_only_the_writes_that_depend_on_it(unarmed, vault):
    """A writable page whose status label the writer cannot classify blocks no unrelated edit.

    An edit of another page still succeeds; an edit whose relation touches the unclassifiable
    page refuses with the shared unavailable error. This catches a corpus-wide refusal of every
    restricted edit, and a relaxation that judges a relation without its endpoint's class.
    """
    from exomem.edit import edit
    from exomem.lifecycle_statuses import ClassificationUnavailable

    _, authenticate = unarmed
    notes = vault / "Knowledge Base/Notes/Insights"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "shelved.md").write_text(
        "---\ntype: insight\nproject: own-matter\nstatus: shelved\n---\n"
        "# Shelved\n\n- [decision] Pause the archive migration.\n", encoding="utf-8",
    )
    (notes / "linked.md").write_text(
        "---\ntype: insight\nproject: own-matter\nstatus: active\n---\n"
        "# Linked\n\n- [decision] Keep retry windows bounded.\n\n## Relations\n- supports [[shelved]]\n",
        encoding="utf-8",
    )
    with principal.request_scope(authenticate("writer")):
        edit(vault, path=OWN_PAGE, why="correct own note", old_string="own text", new_string="corrected own text")
        with pytest.raises(ClassificationUnavailable):
            edit(vault, path="Knowledge Base/Notes/Insights/linked.md", why="tighten the decision",
                 old_string="bounded.", new_string="tightly bounded.")
    assert "corrected own text" in (vault / OWN_PAGE).read_text()
    assert "tightly" not in (notes / "linked.md").read_text()


# ---------------------------------------------------------------------------
# Side effects, workflow state and generated destinations
# ---------------------------------------------------------------------------

LOG = "Knowledge Base/log.md"
TOP_INDEX = "Knowledge Base/index.md"
READABLE_SOURCE = "Knowledge Base/Sources/Shared/shared-original.md"
WITHHELD_SOURCE = "Knowledge Base/Sources/Private/private-original.md"
ABSENT_SOURCE = "Knowledge Base/Sources/Private/absent-original.md"


def _original(vault: Path, path: str) -> bytes:
    page = vault / path
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        f"---\ntype: source\nexomem_id: {uuid.uuid4()}\ningested_into: []\n---\n"
        "# Original\n\nCaptured original text.\n", encoding="utf-8")
    find.clear_cache()
    lexstore.ensure_fresh(vault)
    return page.read_bytes()


ANCHOR = "Knowledge Base/Notes/Insights/anchor.md"


def _remember(vault: Path, who, title: str, **kwargs: Any) -> Any:
    """Remember an insight that relates to a shared active insight, which a note needs to be written."""
    anchor = vault / ANCHOR
    if not anchor.exists():
        anchor.parent.mkdir(parents=True, exist_ok=True)
        anchor.write_text("---\ntype: insight\nstatus: active\nproject: shared-project\n---\n# Anchor\n\nShared anchor text.\n", encoding="utf-8")
    content = ("## Finding\n\nA durable conclusion.\n\n## Observations\n\n- [finding] One bath runs every stage. ^bath\n\n"
               f"## Relations\n\n- relates_to [[{ANCHOR.removesuffix('.md')}]]\n")
    return _call(vault, who, "remember", content=content, title=title, note_type="insight", **kwargs)


@pytest.fixture
def citing(vault, tmp_path, monkeypatch):
    """A path-only world, because a Source is cited through a path-only release that a semantic selector leaves undecided.

    The writer reads `Sources/Shared/` without writing it, is fenced from `Sources/Private/`, and
    writes one inbox per tree. Returns the principal of that writer.
    """
    governance = vault / "Knowledge Base/_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    for scope, folder in ((SHARED_SOURCES, "Shared"), (PRIVATE_SOURCES, "Private")):
        (governance / "scopes" / f"sources-{folder.lower()}.yaml").write_text(
            f'governance_version: 1\nid: {scope}\npaths: ["Sources/{folder}/**"]\n', encoding="utf-8")
    (governance / "rules" / "sources-shared.yaml").write_text(
        f"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB4\nscope_ids: [\"{SHARED_SOURCES}\"]\n"
        f"audience: external\nceiling: {egress.LEVEL_FULL}\n", encoding="utf-8")
    return grant_writer(vault, tmp_path, monkeypatch, writable_paths=list(INBOX_GLOBS), capture_paths=list(INBOX_FOLDERS),
                        protected=[PRIVATE_SOURCES])("external")


def _capture_source(vault: Path, who, title: str) -> dict:
    with principal.request_scope(who):
        return writer_lease.invoke_command(
            _COMMANDS["capture_source"], vault, schema_module.load_source_schema(vault),
            title=title, content="Captured original text.", source_kind="session")


def test_a_fenced_client_cites_a_readable_source_it_may_not_write(citing, vault):
    """The note is written, the Source keeps its bytes, and the answer names the skipped back-reference."""
    source_before = _original(vault, READABLE_SOURCE)

    answer = _remember(vault, citing, "Cites a shared original", sources=[READABLE_SOURCE.removesuffix(".md")])

    assert "__error__" not in answer, answer
    assert any("ingested_into back-ref skipped for 1 cited source" in warning for warning in answer["warnings"]), answer["warnings"]
    assert (vault / READABLE_SOURCE).read_bytes() == source_before


def test_a_withheld_source_cited_by_a_fenced_client_answers_as_a_missing_one(citing, vault):
    """The writer learns nothing about a Source it cannot read: the refusal matches a citation of a missing one."""
    source_before = _original(vault, WITHHELD_SOURCE)

    withheld = _remember(vault, citing, "Cites one original", sources=[WITHHELD_SOURCE.removesuffix(".md")])
    missing = _remember(vault, citing, "Cites another original", sources=[ABSENT_SOURCE.removesuffix(".md")])

    assert withheld == missing
    assert "UNRESOLVED_SOURCE_CITATION" in withheld["message"], withheld
    assert (vault / WITHHELD_SOURCE).read_bytes() == source_before


def test_inbox_creation_leaves_the_activity_log_and_the_top_index_unchanged(confined, vault):
    """Each generated destination skips the shared log and index; the owner's create still writes them."""
    _, authenticate = confined
    writer = authenticate("writer")
    watched = {path: (vault / path).read_bytes() for path in (LOG, TOP_INDEX)}

    created = _call(vault, writer, *_create(NEW_INBOX_PAGE)[:1], **_create(NEW_INBOX_PAGE)[1])
    remembered = _remember(vault, writer, "A note from the inbox")
    captured = _capture_source(vault, writer, "A capture from the inbox")

    assert "__error__" not in created and "__error__" not in remembered, (created, remembered)
    assert captured["path"].startswith(SOURCES_INBOX)
    assert {path: (vault / path).read_bytes() for path in watched} == watched
    owner_created = _call(vault, principal.owner_principal(surface="cli"), "manage_memory_file", operation="create",
                          path="Knowledge Base/Notes/owner-page.md", content="# Owner page\n\nOwner text.\n")
    assert not _refused(owner_created), owner_created
    assert (vault / LOG).read_bytes() != watched[LOG]


def test_a_fenced_remember_lands_in_the_notes_inbox_and_an_owners_stays_in_its_folder(confined, vault):
    _, authenticate = confined

    fenced = _remember(vault, authenticate("writer"), "A fenced insight")
    unfenced = _remember(vault, authenticate("full"), "An unfenced insight")

    assert fenced["path"].startswith(f"{NOTES_INBOX}/Insights/"), fenced
    assert unfenced["path"].startswith("Knowledge Base/Notes/Insights/"), unfenced


def test_a_fenced_client_completes_a_note_that_has_no_typed_relation_in_its_inbox(confined, vault):
    """The draft comes back intact for a creation in the writer's own inbox, so `reviewed_none` can be issued on it.

    A free slot in another client's inbox, or outside every inbox, stays unavailable to the writer.
    """
    _, authenticate = confined
    writer, sibling = authenticate("writer"), authenticate("sibling")
    creation = dict(
        content="## Finding\n\nA durable conclusion.\n\n## Observations\n\n- [finding] One bath runs every stage. ^bath\n",
        title="A note with no typed relation", note_type="insight")

    blocked = _call(vault, writer, "remember", **creation)
    draft = _call(vault, writer, "remember", **creation, validate_only=True)
    written = _call(vault, writer, "remember", **creation, draft_id=draft.get("draft_id"), draft_hash=draft.get("draft_hash"),
                    draft_token=draft.get("draft_token"), relation_disposition="reviewed_none",
                    relation_review_hash=draft.get("draft_hash"),
                    relation_review_reason="A free-standing conclusion; no other page relates to it.")

    assert "RELATION_DISPOSITION_MISSING" in blocked["message"], blocked
    assert draft.get("destination", "").startswith(f"{NOTES_INBOX}/Insights/"), draft
    assert written.get("path") == draft["destination"], written
    assert (vault / written["path"]).is_file()
    free_slot = NEW_INBOX_PAGE
    with principal.request_scope(writer):
        assert egress.content_permits(vault, free_slot, writer)
        assert not egress.content_permits(vault, NEW_OUTSIDE_PAGE, writer)
    with principal.request_scope(sibling):
        assert not egress.content_permits(vault, free_slot, sibling)


def test_a_fenced_source_capture_under_the_sources_inbox_can_be_cited(citing, vault):
    """The captured Source is in the writer's own inbox, so its back-reference is written, not skipped."""
    writer = citing
    captured = _capture_source(vault, writer, "A fenced capture")
    path = captured["path"]
    assert path.startswith(f"{SOURCES_INBOX}/")

    answer = _remember(vault, writer, "Cites its own capture", sources=[path.removesuffix(".md")])

    assert "__error__" not in answer, answer
    assert not any("back-ref skipped" in warning for warning in answer.get("warnings", [])), answer
    assert answer["path"].removesuffix(".md").rsplit("/", 1)[1] in (vault / path).read_text(encoding="utf-8")


def test_a_fenced_episode_records_prepares_and_resumes_without_touching_policy(confined, vault, monkeypatch):
    """The journal under the governance tree is workflow state; no policy document or manifest changes."""
    from exomem import episode_workflow

    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    _, authenticate = confined
    governance = vault / "Knowledge Base/_Governance"
    curation = governance / "curation"
    policy_before = {path: path.read_bytes() for path in governance.rglob("*")
                     if path.is_file() and curation not in path.parents}
    leaf = {"leaf_key": "write", "effect_revision": 1, "kind": "create-note", "args": {
        "title": "Steady bath model", "slug": "steady-bath-model", "content": (
            "## Observations\n\n- [finding] One bath. ^bath\n\n"
            f"## Relations\n\n- relates_to [[{OWN_NOTE.removesuffix('.md')}]]\n")}}
    proposal = {"route": "focused_note", "alternatives": [], "evidence": "complete", "title": "Steady bath model",
                "reason": "A named model none of the existing pages own.", "leaves": [leaf]}

    def episode(**kwargs: Any) -> dict:
        with principal.request_scope(authenticate("writer")):
            return commands.op_episode_memory(vault, schema_module.load_source_schema(vault), episode=_WRITER_KEY, **kwargs)

    recorded = episode(action="record", subject="Dye bath", summary="What was settled.", worked_on=["Worked on the bath"])
    episode(action="prepare", candidate="model", proposal=proposal)
    reviewed = episode(action="disposition", candidate="model", disposition="routed", reason="Reviewed.")
    resumed = episode(action="resume", input_revision=1, journal_digest=reviewed["journal_digest"])

    assert recorded["source"]["path"].startswith(f"{SOURCES_INBOX}/Episodes/")
    assert resumed["status"] == "ok", (resumed["blocked"], resumed["stale"])
    assert all(item["path"].startswith(f"{NOTES_INBOX}/") for item in resumed["executed"]), resumed["executed"]
    assert any(path.is_file() for path in curation.rglob("*"))
    assert {path: path.read_bytes() for path in policy_before} == policy_before


def test_a_move_skips_the_linkers_the_writer_may_not_write_and_names_only_the_readable_ones(confined, vault):
    """A readable linker it cannot write is skipped with a warning; a withheld linker leaves the answer as if absent."""
    _, authenticate = confined
    writer = authenticate("writer")
    inbox = vault / NOTES_INBOX
    inbox.mkdir(parents=True, exist_ok=True)
    for name in ("moved-a", "moved-b", "moved-c"):
        (inbox / f"{name}.md").write_text(f"Inbox text of {name}.\n", encoding="utf-8")
    visible = vault / "Knowledge Base/Notes/visible-linker.md"
    visible.write_text(f"---\nproject: shared-project\n---\nSee [[{NOTES_INBOX}/moved-a]].\n", encoding="utf-8")
    hidden = vault / "Knowledge Base/Notes/hidden-linker.md"
    hidden.write_text(f"---\nproject: private-project\n---\nSee [[{NOTES_INBOX}/moved-b]].\n", encoding="utf-8")
    visible_before, hidden_before = visible.read_bytes(), hidden.read_bytes()
    find.clear_cache()
    lexstore.ensure_fresh(vault)

    def move(name: str) -> Any:
        return _call(vault, writer, "manage_memory_file", operation="move",
                     old_path=f"{NOTES_INBOX}/{name}.md", new_path=f"{NOTES_INBOX}/Archive/{name}.md")

    with_visible, with_hidden, with_none = move("moved-a"), move("moved-b"), move("moved-c")

    assert all("__error__" not in answer for answer in (with_visible, with_hidden, with_none)), (
        with_visible, with_hidden, with_none)
    assert any("1 visible linking page(s) outside your write scope" in warning for warning in with_visible.get("warnings", [])), json.dumps(with_visible)
    def shape(answer: dict, name: str) -> str:
        return json.dumps({key: value for key, value in answer.items() if key != "request_id"},
                          sort_keys=True).replace(name, "<name>")

    assert shape(with_hidden, "moved-b") == shape(with_none, "moved-c")
    assert (visible.read_bytes(), hidden.read_bytes()) == (visible_before, hidden_before)
