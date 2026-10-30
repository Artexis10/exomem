"""The generic-relation barrier, end to end (`promote-referents-without-nudges` task 2.8).

A write that adds a generic edge to an entity, where a registered specific relation
fits both pages, stops before it commits. The caller names a fitting relation or keeps
the edge generic with a reason. Every case goes through the public tools over an
initialized vault; the restricted-caller case calls the same writer in process.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from test_governance_egress import _external, write_rule, write_scope
from test_relation_endpoints import _entity, _served

from exomem import commands, note
from exomem import vault as vault_module
from exomem.governance.principal import request_scope

CODE = "GENERIC_RELATION_NEEDS_REASON"
REASON = "They crossed paths once, which no listed relation says."
# Page ids are random per vault, and the digests taken over them differ with them.
_RANDOM_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{64}")


def _source(call, title: str) -> str:
    captured = call(
        "capture_source",
        {"title": f"{title} source", "source_kind": "session", "content": f"{title} was discussed."},
    )
    return f"{captured['path']}#Capture"


def _body(title: str, *rows: str) -> str:
    relations = "".join(f"- {row}\n" for row in rows)
    return f"## Observations\n\n- [fact] {title} was settled.\n" + (
        f"\n## Relations\n\n{relations}" if rows else ""
    )


def _create(call, title: str, source: str, *rows: str, **extra) -> dict:
    return call(
        "create_note",
        {
            "title": title,
            "note_type": "insight",
            "content": _body(title, *rows),
            "sources": [source],
            **extra,
        },
    )


def _shown(result: dict, vault: Path) -> str:
    """A result as text that two vaults share when they disclose the same thing.

    The vault root and the page ids differ per vault by construction, and the writer lists
    its facts in the order of those ids, so list order is ignored too; nothing else may differ.
    """

    def unordered(value):
        if isinstance(value, dict):
            return {key: unordered(item) for key, item in value.items()}
        if isinstance(value, list):
            return sorted(
                (unordered(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True)
            )
        return value

    text = json.dumps(result, sort_keys=True, default=str).replace(str(vault), "<vault>")
    return json.dumps(unordered(json.loads(_RANDOM_ID.sub("<id>", text))), sort_keys=True)


def _target(entity: dict) -> str:
    return entity["path"].removesuffix(".md")


def _logged_edges(vault: Path, path: str) -> list[dict]:
    """The edges the operation log records as added by the one write of `path`."""
    entries = [
        entry["provenance"]
        for entry in vault_module.read_operation_log(vault)["entries"]
        if entry.get("provenance", {}).get("path") == path
    ]
    assert len(entries) == 1, entries
    return entries[0]["added_edges"]


def _anchored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A served vault whose first compiled note exists, so later notes face the full contract."""
    vault, call = _served(tmp_path, monkeypatch)
    anchor = _create(call, "Anchor", _source(call, "Anchor"))
    assert anchor["state"] == "committed", anchor
    return vault, call, anchor["path"].removesuffix(".md")


def test_a_generic_edge_to_an_entity_is_refused_and_one_reasoned_retry_commits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, _anchor = _anchored(tmp_path, monkeypatch)
    ben = _target(_entity(call, "person", "Ben Quay"))
    source = _source(call, "Crane")
    row = f"relates_to [[{ben}]]"

    refused = _create(call, "Crane", source, row)

    error = refused["error"]
    assert error["code"] == CODE
    assert len(error["options"]) <= 4
    assert "about_entity" in [option["key"] for option in error["options"]]
    assert all(set(option) == {"key", "label", "description", "parent"} for option in error["options"])
    assert not (vault / "Knowledge Base/Notes/Insights/crane.md").exists()

    retry = {key: error[key] for key in ("draft_id", "draft_token", "draft_hash")}
    committed = _create(
        call,
        "Crane",
        source,
        row,
        relation_disposition="generic",
        relation_review_reason=REASON,
        relation_review_hash=error["relation_review_hash"],
        **retry,
    )

    assert committed["state"] == "committed", committed
    [edge] = _logged_edges(vault, committed["path"])
    assert (edge["relation"], edge["generic"]) == ("relates_to", True)
    assert (edge["reason"], edge["reason_recorded"]) == (REASON, True)


def test_naming_a_fitting_relation_commits_without_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, _anchor = _anchored(tmp_path, monkeypatch)
    ben = _target(_entity(call, "person", "Ben Quay"))
    source = _source(call, "Crane")
    assert _create(call, "Crane", source, f"relates_to [[{ben}]]")["error"]["code"] == CODE

    committed = _create(call, "Crane", source, f"about_entity [[{ben}]]")

    assert committed["state"] == "committed", committed
    [edge] = _logged_edges(vault, committed["path"])
    assert (edge["relation"], edge["generic"]) == ("about_entity", False)
    assert (edge["reason"], edge["reason_recorded"]) == (None, False)


def test_an_edit_adding_a_generic_edge_is_refused_and_a_later_edit_of_the_kept_edge_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, anchor = _anchored(tmp_path, monkeypatch)
    ben = _target(_entity(call, "person", "Ben Quay"))
    page = _create(call, "Crane", _source(call, "Crane"), f"supports [[{anchor}]]")["path"]
    operation = {
        "kind": "replace_string",
        "old_string": f"- supports [[{anchor}]]",
        "new_string": f"- supports [[{anchor}]]\n- relates_to [[{ben}]]",
    }

    refused = call("edit_note", {"path": page, "why": "link the person", "operation": operation})

    error = refused["error"]
    assert error["code"] == CODE
    assert "about_entity" in [option["key"] for option in error["options"]]
    assert f"relates_to [[{ben}]]" not in (vault / page).read_text(encoding="utf-8")

    kept = call(
        "edit_note",
        {
            "path": page,
            "why": "link the person",
            "operation": {
                **operation,
                "transition_token": error["transition_token"],
                "relation_disposition": "generic",
                "relation_review_reason": REASON,
                "relation_review_hash": error["relation_review_hash"],
            },
        },
    )
    assert kept["state"] == "committed", kept

    # The edge now stands on the page; touching other text is not adding it again.
    later = call(
        "edit_note",
        {
            "path": page,
            "why": "fix the wording",
            "operation": {
                "kind": "replace_string",
                "old_string": "was settled.",
                "new_string": "was settled for good.",
            },
        },
    )
    assert later["state"] == "committed", later


def test_a_page_that_is_not_live_and_an_adoption_import_are_never_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, _anchor = _anchored(tmp_path, monkeypatch)
    ben = _target(_entity(call, "person", "Ben Quay"))
    source = _source(call, "Crane")
    row = f"relates_to [[{ben}]]"

    draft = _create(call, "Draft crane", _source(call, "Draft crane"), row, status="draft")
    assert draft["state"] == "committed", draft

    # `adoption_compile` has no tool door; the same page is checked under both operations.
    def validate(operation: str):
        return note.note(
            vault,
            content=_body("Adopted crane", row),
            note_type="insight",
            title="Adopted crane",
            sources=[source],
            validate_only=True,
            _preflight_operation=operation,
        )

    assert validate("adoption_compile").draft_hash
    with pytest.raises(note.NoteError) as refused:
        validate("create")
    assert refused.value.code == CODE


def test_a_successor_is_not_asked_about_the_edge_it_carries_but_about_one_it_adds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, _anchor = _anchored(tmp_path, monkeypatch)
    ben = _target(_entity(call, "person", "Ben Quay"))
    cy = _target(_entity(call, "person", "Cy Dock"))
    source = _source(call, "Crane")
    row = f"relates_to [[{ben}]]"
    first = _create(call, "Crane", source, row)["error"]
    old = _create(
        call,
        "Crane",
        source,
        row,
        relation_disposition="generic",
        relation_review_reason=REASON,
        relation_review_hash=first["relation_review_hash"],
        **{key: first[key] for key in ("draft_id", "draft_token", "draft_hash")},
    )["path"]

    def supersede(old_path: str, title: str, *rows: str) -> dict:
        return call(
            "supersede_note",
            {
                "old_path": old_path,
                "title": title,
                "note_type": "insight",
                "content": _body(title, *rows),
                "sources": [source],
            },
        )

    carried = supersede(old, "Crane revised", row)
    assert carried["state"] == "committed", carried

    other = _create(call, "Hoist", _source(call, "Hoist"), f"about_entity [[{ben}]]")["path"]
    added = supersede(other, "Hoist revised", f"about_entity [[{ben}]]", f"relates_to [[{cy}]]")
    assert added["error"]["code"] == CODE


def test_a_withheld_target_gives_a_restricted_writer_the_result_an_absent_one_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def written_by_restricted(name: str, *, entity_exists: bool, withheld: bool):
        vault, call, anchor = _anchored(tmp_path / name, monkeypatch)
        if entity_exists:
            _entity(call, "person", "Ben Quay")
        if withheld:
            write_scope(vault, paths="Entities/People/**", name="People")
            write_rule(vault, ceiling=0)
        source = _source(call, "Crane")
        content = _body(
            "Crane", f"supports [[{anchor}]]", "relates_to [[Knowledge Base/Entities/People/Ben Quay]]"
        )
        with request_scope(_external()):
            try:
                result = commands.op_remember(
                    vault, content=content, title="Crane", note_type="insight", sources=[source]
                )
            except ValueError as error:
                result = {"refused": str(error)}
        return _shown(result, vault)

    visible = written_by_restricted("visible", entity_exists=True, withheld=False)
    withheld = written_by_restricted("withheld", entity_exists=True, withheld=True)
    absent = written_by_restricted("absent", entity_exists=False, withheld=True)

    # The control: a restricted writer who may see the entity is asked.
    assert CODE in visible
    assert withheld == absent
    assert CODE not in withheld


def test_create_entity_commits_a_generic_item_that_carries_a_reason_and_logs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    works = _target(_entity(call, "organization", "Quay Works"))
    item = {"kind": "relates_to", "target": works}

    refused = _entity(call, "person", "Cy Dock", relations=[item])
    assert refused["error"]["code"] == CODE
    assert not (vault / "Knowledge Base/Entities/People/Cy Dock.md").exists()

    committed = _entity(call, "person", "Cy Dock", relations=[{**item, "reason": REASON}])

    assert committed["state"] == "committed", committed
    [edge] = _logged_edges(vault, committed["path"])
    assert (edge["reason"], edge["reason_recorded"]) == (REASON, True)


def test_a_reason_on_a_specific_item_does_not_answer_a_generic_item_to_the_same_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call = _served(tmp_path, monkeypatch)
    works = _target(_entity(call, "organization", "Quay Works"))
    specific = {"kind": "member_of", "target": works, "reason": REASON}
    generic = {"kind": "relates_to", "target": works}

    refused = _entity(call, "person", "Cy Dock", relations=[specific, generic])
    committed = _entity(call, "person", "Cy Dock", relations=[specific, {**generic, "reason": REASON}])

    assert refused["error"]["code"] == CODE
    assert committed["state"] == "committed", committed


def test_a_person_is_offered_the_relations_that_fit_a_person_or_an_organization_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call = _served(tmp_path, monkeypatch)
    works = _target(_entity(call, "organization", "Quay Works"))
    ben = _target(_entity(call, "person", "Ben Quay"))

    def offered(target: str) -> list[str]:
        refused = _entity(call, "person", "Cy Dock", relations=[{"kind": "relates_to", "target": target}])
        return [option["key"] for option in refused["error"]["options"]]

    assert offered(works) == ["member_of", "affiliated_with", "owns", "operates"]
    assert offered(ben) == ["kin_of", "owns", "operates", "supplies"]


def test_a_generic_edge_between_two_concepts_commits_without_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call = _served(tmp_path, monkeypatch)
    ledger = _target(_entity(call, "concept", "Tidal Ledger"))

    committed = _entity(
        call, "concept", "Tide Table", relations=[{"kind": "relates_to", "target": ledger}]
    )

    assert committed["state"] == "committed", committed


def test_a_plain_connection_between_two_people_is_refused_and_kin_of_is_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call = _served(tmp_path, monkeypatch)
    ben = _target(_entity(call, "person", "Ben Quay"))

    refused = _entity(call, "person", "Cy Dock", connections=[f"[[{ben}]]"])

    assert refused["error"]["code"] == CODE
    assert "kin_of" in [option["key"] for option in refused["error"]["options"]]


def test_a_generic_edge_between_two_notes_is_never_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call, anchor = _anchored(tmp_path, monkeypatch)

    committed = _create(call, "Crane", _source(call, "Crane"), f"relates_to [[{anchor}]]")

    assert committed["state"] == "committed", committed
