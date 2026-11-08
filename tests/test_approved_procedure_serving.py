"""Serving surfaces report the approval verdict (add-approved-procedures 3.1 to 3.4 and 3.6).

Each test runs the real activation and read paths over a real vault in a temporary directory.
A page is eligible through the real semantic contract. All data is invented.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from test_activate_context_maps import _activate
from test_connector_boundary import configured_boundary as configured_boundary
from test_context_catalogue import _built, _page
from test_governance_egress import _external
from test_working_set_withheld_twins import _withhold

import exomem
from exomem import (
    commands,
    context_task,
    freshness,
    lexstore,
    procedure_approval,
    reserved_paths,
    working_set_runtime,
)
from exomem.governance import principal
from exomem.writer_lease import invoke_command

REF_ID = "6b1f2c3e-0d4a-4e5f-8a9b-1c2d3e4f5a6b"
REF = f"exomem://memory/{REF_ID}"
TURN = "Inspect the optical alignment before the workshop"
STEP = "Inspect optical alignment against the reference target"


@pytest.fixture(autouse=True)
def _maps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")


def _procedure(vault: Path, name: str = "alignment-method", *, ref_id: str | None = REF_ID) -> str:
    frontmatter = f"exomem_id: {ref_id}\n" if ref_id else ""
    return _page(vault, name, frontmatter, body=f"- [technique] {STEP} ^alignment\n\nExclude handheld field checks.\n")


def _index(vault: Path) -> None:
    """Make the page recallable: the catalogue and the lexical index both read the files."""
    _built(vault).close()
    lexstore.ensure_fresh(vault)


def _approve(vault: Path, rel: str, ref: str = REF) -> procedure_approval.Outcome:
    facts = procedure_approval.page_facts(vault, rel, (vault / rel).read_bytes())
    with reserved_paths._owner_authority_scope("approve_procedure"):  # noqa: SLF001 - the dispatcher's scope
        return procedure_approval.approve(
            vault, ref, facts, expected_version=facts.version, principal="owner", client="fixture client",
        )


def _revoke(vault: Path, rel: str) -> None:
    version = procedure_approval.page_facts(vault, rel, (vault / rel).read_bytes()).version
    with reserved_paths._owner_authority_scope("approve_procedure"):  # noqa: SLF001 - the dispatcher's scope
        procedure_approval.revoke(
            vault, REF, current_version=version, expected_version=version, principal="owner", client="fixture client",
        )


def _method(packet: dict, rel: str) -> dict:
    return next(item for item in packet["task_context"]["methods"]["items"] if item["source_ref"] == rel)


def test_an_approved_method_carries_one_title_line_and_none_of_the_steps(vault: Path) -> None:
    """A fresh client learns that the owner endorsed the method, without the packet quoting its steps."""
    rel = _procedure(vault)
    _index(vault)
    _approve(vault, rel)
    packet = _activate(vault, turn=TURN, max_chars=8000)
    item = _method(packet, rel)
    assert item["approval"] == "approved"
    line = context_task.item_line(item, method=True)
    assert line == f"Approved procedure: {item['attribution']}. Read it, then follow it where it fits."
    assert STEP not in line and STEP not in str(packet["task_context"])


def _hook():
    script = Path(exomem.__file__).parent / "_hooks" / "exomem_retrieve_nudge.py"
    spec = importlib.util.spec_from_file_location("exomem_retrieve_nudge_approval_under_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_hook_renders_an_approved_method_as_its_own_line_and_never_a_link(vault: Path) -> None:
    """The hook tells the agent the owner endorsed a method, and an unapproved method keeps the plain wording."""
    rel = _procedure(vault)
    _index(vault)
    unapproved = _activate(vault, turn=TURN, max_chars=8000)
    _approve(vault, rel)
    approved = _activate(vault, turn=TURN, max_chars=8000)
    hook = _hook()
    plain_text = hook._format_task_context(unapproved["task_context"], 4000)  # noqa: SLF001 - the render under test
    approved_text = hook._format_task_context(approved["task_context"], 4000)  # noqa: SLF001
    title = _method(approved, rel)["attribution"]
    assert f"- method: {title}; read the source for applicability and exclusions." in plain_text
    assert "approved procedure" not in plain_text
    assert f"- approved procedure: {title}. Read it, then follow it where it fits." in approved_text
    assert "- method:" not in approved_text
    assert STEP not in approved_text and "link" not in approved_text


def _read(vault: Path, rel: str, caller=None, **kwargs) -> dict:
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "read_memory")
    with principal.request_scope(caller or principal.owner_principal(surface="library")):
        return invoke_command(command, vault, path=rel, **kwargs)


def _edit_directly(vault: Path, rel: str) -> None:
    """Change the bytes the way an editor does: no operation, no ledger entry."""
    page = vault / rel
    page.write_text(page.read_text().replace("Exclude handheld field checks.", "Exclude all handheld and workshop checks."))
    freshness.on_files_changed(vault, [page])
    _index(vault)


def _without_approval_hints(token: str) -> str:
    """Rewrite a continuity token the way a server that predates approvals wrote it."""
    import base64
    import json

    payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    for hint in payload["method_versions"]:
        del hint["approval"]
    return working_set_runtime._pack(payload)  # noqa: SLF001 - the token's own writer


def test_a_revoke_with_unchanged_bytes_resets_the_next_activation_of_a_held_method(vault: Path) -> None:
    """A client that holds an approved method must not keep it after the owner withdraws the approval."""
    rel = _procedure(vault)
    _index(vault)
    _approve(vault, rel)
    first = _activate(vault, turn=TURN, max_chars=8000)
    held = working_set_runtime.decode_continuity(first["continuity"])["method_versions"]
    assert [hint["approval"] for hint in held] == ["approved"]
    unchanged = _activate(vault, turn=TURN, max_chars=8000, continuity=first["continuity"])
    assert unchanged["task_context"]["methods"]["reset"] is False

    _revoke(vault, rel)
    after = _activate(vault, turn=TURN, max_chars=8000, continuity=first["continuity"])
    assert after["task_context"]["methods"]["reset"] is True
    assert _method(after, rel)["approval"] == "unapproved"
    renewed = working_set_runtime.decode_continuity(after["continuity"])["method_versions"]
    assert [hint["approval"] for hint in renewed] == ["unapproved"]


def test_a_token_without_approval_hints_reads_unapproved(vault: Path) -> None:
    """A client that holds a token from before approvals keeps an unapproved method and resets an approved one."""
    rel = _procedure(vault)
    _index(vault)
    plain = _activate(vault, turn=TURN, max_chars=8000)
    older = _without_approval_hints(plain["continuity"])
    assert [hint["approval"] for hint in working_set_runtime.decode_continuity(older)["method_versions"]] == ["unapproved"]
    assert _activate(vault, turn=TURN, max_chars=8000, continuity=older)["task_context"]["methods"]["reset"] is False

    _approve(vault, rel)
    assert _activate(vault, turn=TURN, max_chars=8000, continuity=older)["task_context"]["methods"]["reset"] is True


def test_a_direct_edit_makes_the_approval_stale_for_the_owner_and_unapproved_for_everyone_else(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes that changed outside the operation are no longer what the owner endorsed, on every surface."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    other = _page(vault, "private-reference", body="Restricted inspection reference.\n")
    rel = _procedure(vault)
    _withhold(vault, other, ceiling=0)
    _index(vault)
    version = _approve(vault, rel).appended.version
    assert _method(_activate(vault, turn=TURN, max_chars=8000), rel)["approval"] == "approved"
    assert _read(vault, rel)["approval"] == "approved"

    _edit_directly(vault, rel)
    guest = _activate(vault, _external(), turn=TURN, max_chars=8000)
    assert _method(_activate(vault, turn=TURN, max_chars=8000), rel)["approval"] == "stale"
    assert _read(vault, rel)["approval"] == "stale"
    assert _method(guest, rel)["approval"] == "unapproved"
    assert "approval" not in _read(vault, rel, _external())
    assert version not in str(guest) and "fixture client" not in str(guest)


def test_a_restricted_reader_sees_approved_or_unapproved_and_never_the_entry(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader that is not the owner learns the owner's endorsement and nothing about who gave it."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    other = _page(vault, "private-reference", body="Restricted inspection reference.\n")
    rel = _procedure(vault)
    _withhold(vault, other, ceiling=0)
    _index(vault)
    entry = _approve(vault, rel).appended
    guest = _activate(vault, _external(), turn=TURN, max_chars=8000)
    assert _method(guest, rel)["approval"] == "approved"
    reading = _read(vault, rel, _external())
    assert reading["approval"] == "approved"
    assert "diagnostics" not in reading
    # The page's version is its content_hash, which a full reader receives anyway; the entry's author is not.
    for secret in (entry.principal, entry.client, entry.entry_id):
        assert secret not in str(guest) and secret not in str(reading)


def test_a_fenced_owner_connection_sees_two_states_and_no_finding(
    vault: Path, configured_boundary, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner behind a connector that denies a scope reads like a restricted reader on every surface."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_RELEASED_CORPUS_TASKS", "on")
    _config, authenticate = configured_boundary
    limited = authenticate("limited")
    rel = _procedure(vault)
    _index(vault)
    _approve(vault, rel)
    assert _read(vault, rel, limited)["approval"] == "approved"

    _edit_directly(vault, rel)
    assert _read(vault, rel)["approval"] == "stale"
    assert "approval" not in _read(vault, rel, limited)
    assert _method(_activate(vault, limited, turn=TURN, max_chars=8000), rel)["approval"] == "unapproved"
    entry_file = next((vault / "Knowledge Base" / "_Approvals" / REF_ID).iterdir())
    entry_file.write_text("not json {")
    assert [finding["ref"] for finding in _read(vault, rel)["diagnostics"]["approval_findings"]] == [REF]
    fenced = _read(vault, rel, limited)
    assert "diagnostics" not in fenced and "approval" not in fenced


def test_a_reader_below_full_release_sees_no_endorsement(vault: Path) -> None:
    """A page that the reader gets only as an excerpt carries no endorsement for that reader."""
    rel = _procedure(vault)
    _index(vault)
    _approve(vault, rel)
    assert _read(vault, rel)["approval"] == "approved"
    _withhold(vault, rel, ceiling=5)
    excerpt = _read(vault, rel, _external())
    assert excerpt["release_level"] < 6
    assert "approval" not in excerpt


def test_approval_does_not_change_which_methods_are_served_or_their_order(vault: Path) -> None:
    """The owner's endorsement is information for the reader, never a ranking signal."""
    first = _procedure(vault, "alignment-method-a", ref_id="0c7d1a52-93be-4f1e-b2a6-7d5e8f9a0b1c")
    second = _procedure(vault, "alignment-method-b")
    _index(vault)
    plain = _activate(vault, turn=TURN, max_chars=8000)
    _approve(vault, second)
    ranked = _activate(vault, turn=TURN, max_chars=8000)
    served = [item["source_ref"] for item in plain["task_context"]["methods"]["items"]]
    assert {first, second} <= set(served)
    assert [item["source_ref"] for item in ranked["task_context"]["methods"]["items"]] == served
    assert [item["source_ref"] for item in ranked["task_context"]["candidates"]] == [
        item["source_ref"] for item in plain["task_context"]["candidates"]
    ]
    assert {item["source_ref"]: item["approval"] for item in ranked["task_context"]["methods"]["items"]} == {
        first: "unapproved", second: "approved",
    }


def test_the_shared_allowance_is_charged_for_the_approved_line(vault: Path) -> None:
    """Approving a method changes the packet's charge by exactly the approved line against the plain one."""
    rel = _procedure(vault)
    _index(vault)
    before = _activate(vault, turn=TURN, max_chars=8000)
    _approve(vault, rel)
    after = _activate(vault, turn=TURN, max_chars=8000)
    item = _method(after, rel)
    plain = context_task.item_line({**item, "approval": "unapproved"}, method=True)
    assert _method(before, rel)["approval"] == "unapproved" and item["approval"] == "approved"
    assert after["budget"]["used_chars"] - before["budget"]["used_chars"] == len(context_task.item_line(item, method=True)) - len(plain)
    assert after["budget"]["used_chars"] <= after["budget"]["limit_chars"]


def test_the_serving_paths_never_scan_the_vault_for_holders(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An approved page costs the catalogue's rows, never a walk of the whole corpus, on every request."""
    rel = _procedure(vault)
    _index(vault)
    _approve(vault, rel)

    def scan(*_args, **_kwargs):
        raise AssertionError("a serving path scanned for holders")

    monkeypatch.setattr("exomem.memory_refs.paths_for_ids_read_only", scan)
    assert _method(_activate(vault, turn=TURN, max_chars=8000), rel)["approval"] == "approved"
    assert _read(vault, rel)["approval"] == "approved"


def test_a_read_parses_the_page_for_its_verdict_only_when_an_approval_entry_needs_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every read of a page with an id asks for a verdict, so a page nobody approved must not cost a parse."""
    rel = _procedure(vault)
    _index(vault)
    parses = []
    real = procedure_approval.page_facts
    monkeypatch.setattr(procedure_approval, "page_facts", lambda *args, **kwargs: parses.append(args[1]) or real(*args, **kwargs))
    assert "approval" not in _read(vault, rel)
    assert parses == []
    _approve(vault, rel)
    parses.clear()
    assert _read(vault, rel)["approval"] == "approved"
    assert len(parses) == 1
    _revoke(vault, rel)
    parses.clear()
    assert _read(vault, rel)["approval"] == "unapproved"
    assert parses == []


def test_a_page_approved_after_the_catalogue_last_folded_reads_approved_on_both_surfaces(vault: Path) -> None:
    """The owner reads back the approval just given, though no map or activation has folded the new page yet."""
    _procedure(vault)
    _index(vault)
    newer_id = "7c2f3d4e-1e5b-4f60-9bac-2d3e4f5a6b7c"
    newer = _procedure(vault, "alignment-method-newer", ref_id=newer_id)
    lexstore.ensure_fresh(vault)
    _approve(vault, newer, f"exomem://memory/{newer_id}")
    assert _read(vault, newer)["approval"] == "approved"
    packet = _activate(vault, turn=TURN, max_chars=8000)
    assert _method(packet, newer)["approval"] == "approved"


def test_a_copy_that_repeats_the_id_inherits_no_approval_on_either_surface(vault: Path) -> None:
    """A page copied with its exomem_id and bytes must not borrow the original's endorsement."""
    rel = _procedure(vault)
    _index(vault)
    _approve(vault, rel)
    copy = _procedure(vault, "alignment-method-copy")
    _index(vault)
    # The copy repeats the id, so no page can claim the ref: both read unapproved.
    packet = _activate(vault, turn=TURN, max_chars=8000)
    assert {_method(packet, path)["approval"] for path in (rel, copy)} == {"unapproved"}
    # A repeated id gives a direct read no ref to report, so it reports no approval either.
    assert not {"ref", "approval"} & set(_read(vault, rel)) and not {"ref", "approval"} & set(_read(vault, copy))


def test_a_damaged_entry_fails_its_own_ref_and_shows_the_owner_one_finding(vault: Path) -> None:
    """The owner's read names the damage; any other reader gets the plain verdict and no finding."""
    rel = _procedure(vault)
    _index(vault)
    _approve(vault, rel)
    entry_file = next((vault / "Knowledge Base" / "_Approvals" / REF_ID).iterdir())
    entry_file.write_text(entry_file.read_text().replace(_approve_version(vault, rel), "0" * 64))
    owner = _read(vault, rel)
    assert owner["approval"] == "unapproved"
    assert [finding["ref"] for finding in owner["diagnostics"]["approval_findings"]] == [REF]
    guest = _read(vault, rel, _external())
    assert "diagnostics" not in guest and "approval" not in guest


def _approve_version(vault: Path, rel: str) -> str:
    return procedure_approval.page_facts(vault, rel, (vault / rel).read_bytes()).version


def test_a_page_whose_ref_has_no_ledger_entry_carries_no_approval_and_a_revoked_one_does(vault: Path) -> None:
    """Only the owner sees that a ref has history, so an ordinary note gains no field and no reader can tell
    a revoked procedure from one nobody approved."""
    ordinary = _page(vault, "plain-note", "exomem_id: 3a9d5b71-6c2e-4f08-8d14-0e7b2c9a5f13\n", body="Neutral observation.\n")
    rel = _procedure(vault)
    _index(vault)
    assert "approval" not in _read(vault, ordinary) and "approval" not in _read(vault, rel)
    _approve(vault, rel)
    _revoke(vault, rel)
    assert _read(vault, rel)["approval"] == "unapproved"
    assert "approval" not in _read(vault, rel, _external())


def test_a_page_without_a_stable_ref_reports_no_approval(vault: Path) -> None:
    """Only a page that reports a ref can hold an approval, so an ordinary read gains no field."""
    rel = _procedure(vault, ref_id=None)
    assert "approval" not in _read(vault, rel)
