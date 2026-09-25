"""Single-page read/write doors open pages named in Unicode NFD form.

A vault synced from macOS keeps decomposed (NFD) file names on a byte-exact
file system (Linux ext4): `cafe-e-nfd.md` on disk is not the same name as its
NFKC (composed) spelling. `find_corpus`'s whole-vault walk already handles
this (it opens the spelling it found, via `physical=True`), but the
single-path doors -- `get`/`read_memory` and `move_file`'s source -- opened
only the NFKC spelling and reported the page missing. See
`reserved_paths.resolve_physical_relative`.

`edit` and `replace` resolve a path before validation, authorization and any
dry run, so they refuse an NFD-named page (`NON_CANONICAL_NAME`) rather than
rename it as a side effect; `move_file` onto the same path canonicalizes it.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands, edit as edit_module, move_file as move_module
from exomem import replace as replace_module, server
from exomem.governance import egress, policy
from exomem.governance.principal import RequestPrincipal, request_scope

TODAY = dt.date(2026, 5, 24)

# "café" composed (NFC == NFKC for this character: U+00E9 LATIN SMALL LETTER
# E WITH ACUTE) vs. decomposed (NFD: "e", U+0065, followed by a combining
# acute accent, U+0301) -- different byte sequences on a byte-exact file
# system.
_COMPOSED_NAME = "café-nfd-probe.md"
_DECOMPOSED_NAME = unicodedata.normalize("NFD", _COMPOSED_NAME)
assert _COMPOSED_NAME != _DECOMPOSED_NAME
assert unicodedata.normalize("NFKC", _COMPOSED_NAME) == _COMPOSED_NAME

_DIRECTORY = ("Knowledge Base", "Notes", "Insights")
_DIRECTORY_REL = "/".join(_DIRECTORY)
# An existing fixture insight, so the `relates_to` link below resolves to a
# real page -- satisfying the semantic contract's relation requirement on a
# write (edit/move/replace), not just a read.
_RELATION_TARGET = f"{_DIRECTORY_REL}/rrf-fusion-beats-score-normalization"


def _page_text(body: str) -> str:
    return (
        "---\ntype: insight\nstatus: active\ncreated: 2026-05-10\nupdated: 2026-05-10\n"
        "sources: []\nprojects: [project-alpha]\ntags: [nfd-probe]\n---\n\n"
        f"# NFD probe\n\n## Claim\n\n{body}\n\n"
        "## Relations\n\n"
        f"- relates_to [[{_RELATION_TARGET}]]\n"
    )


def _write_page(directory: Path, name: str, *, body: str = "probe body") -> Path:
    """A schema-valid `insight` page -- satisfies the semantic contract's
    relation and non-empty-unit requirements (unlike a bare frontmatter
    stub), so it survives edit/move/replace's write-time validation, not just
    a read."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(_page_text(body), encoding="utf-8")
    return target


def _nfd_page(vault: Path, *, body: str = "probe body") -> str:
    """Write a page under its composed name, then rename it to NFD on disk.

    Mirrors a vault synced from macOS: the content is ordinary, only the
    on-disk leaf spelling is decomposed. Returns the logical (NFKC) relative
    path a caller would naturally type.
    """
    directory = vault.joinpath(*_DIRECTORY)
    composed = _write_page(directory, _COMPOSED_NAME, body=body)
    composed.rename(directory / _DECOMPOSED_NAME)
    return f"{_DIRECTORY_REL}/{_COMPOSED_NAME}"


def _external() -> RequestPrincipal:
    return RequestPrincipal(audience_id="external", surface="mcp")


def _govern_deny(vault: Path) -> None:
    """Deny the `external` audience for everything under `Notes/`."""
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "notes.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        "name: Notes\n"
        'paths: ["Notes/**"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "external.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        "audience: external\n"
        f"ceiling: {egress.LEVEL_NONE}\n",
        encoding="utf-8",
    )
    policy._CACHE.clear()
    egress.clear_decision_memo()


def _rest_client(vault: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in (
        "EXOMEM_REST_API_KEY", "EXOMEM_UPLOAD_TOKEN",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN", "EXOMEM_CF_ACCESS_AUD",
    ):
        monkeypatch.delenv(leaky, raising=False)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    mcp = server.build_server(require_auth=False)
    return TestClient(mcp.http_app())


# ---------------- readable + movable through op_* ----------------


def test_nfd_named_page_is_readable_via_op_get(vault: Path) -> None:
    rel = _nfd_page(vault)
    out = commands.op_get(vault, path=rel)
    assert "probe body" in out["body"]
    assert out["frontmatter"]["type"] == "insight"


def test_nfd_named_page_is_readable_via_get_frontmatter(vault: Path) -> None:
    rel = _nfd_page(vault)
    out = commands.op_get(vault, path=rel, frontmatter_only=True)
    assert out["frontmatter"]["type"] == "insight"


def test_nfd_named_page_is_movable_via_move_file(vault: Path) -> None:
    rel = _nfd_page(vault)
    directory = vault.joinpath(*_DIRECTORY)
    dst_rel = f"{_DIRECTORY_REL}/moved-from-nfd.md"

    result = move_module.move_file(
        vault, old_path=rel, new_path=dst_rel, today=TODAY, update_wikilinks=False
    )

    assert result.new_path == dst_rel
    assert not (directory / _COMPOSED_NAME).exists()
    assert not (directory / _DECOMPOSED_NAME).exists()
    assert (vault / dst_rel).exists()
    assert "probe body" in (vault / dst_rel).read_text(encoding="utf-8")


# ---------------- readable through the REST door ----------------


def test_nfd_named_page_is_readable_via_rest_read_memory(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = _nfd_page(vault)
    client = _rest_client(vault, monkeypatch)
    r = client.post(
        "/api/read_memory",
        json={"path": rel},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert "probe body" in body["body"]


def test_nfd_named_page_edit_via_rest_is_refused_without_renaming(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = _nfd_page(vault)
    client = _rest_client(vault, monkeypatch)
    r = client.post(
        "/api/edit_memory",
        json={
            "path": rel,
            "why": "REST NFD edit probe",
            "new_body": _page_text("rest edited").split("---\n\n", 1)[1],
        },
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code != 200, r.text
    assert "NON_CANONICAL_NAME" in r.text

    directory = vault.joinpath(*_DIRECTORY)
    assert (directory / _DECOMPOSED_NAME).exists()
    assert not (directory / _COMPOSED_NAME).exists()


# ---------------- ambiguous collision is refused ----------------


def test_ambiguous_nfc_and_nfd_collision_is_refused(vault: Path) -> None:
    directory = vault.joinpath(*_DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True)
    # Two physically distinct files -- one NFC/NFKC-composed, one NFD-decomposed
    # -- that both normalize to the same logical name.
    _write_page(directory, _COMPOSED_NAME, body="composed body")
    _write_page(directory, _DECOMPOSED_NAME, body="decomposed body")

    with pytest.raises(ValueError, match="^AMBIGUOUS_PATH:"):
        commands.op_get(vault, path=f"{_DIRECTORY_REL}/{_COMPOSED_NAME}")


# ---------------- withheld NFD page still reads as missing ----------------


def test_withheld_nfd_page_reads_as_missing_to_restricted_caller(vault: Path) -> None:
    """A denied caller sees the same NOT_FOUND whether the page exists (under
    its NFD spelling) or not -- proving the physical-spelling fallback in
    `annotate_page`'s swap-check doesn't leak content the release decision,
    made on the canonical path before any bytes are opened, has withheld."""
    rel = _nfd_page(vault)
    _govern_deny(vault)

    with request_scope(_external()), pytest.raises(ValueError) as withheld:
        commands.op_get(vault, path=rel)

    directory = vault.joinpath(*_DIRECTORY)
    (directory / _DECOMPOSED_NAME).unlink()
    with request_scope(_external()), pytest.raises(ValueError) as absent:
        commands.op_get(vault, path=rel)

    assert str(withheld.value) == str(absent.value)


# ---------------- review follow-ups ----------------


def _collision(vault: Path) -> tuple[str, Path]:
    """An NFC page and its NFD twin, both on disk under one logical name."""
    directory = vault.joinpath(*_DIRECTORY)
    _write_page(directory, _COMPOSED_NAME, body="composed body")
    _write_page(directory, _DECOMPOSED_NAME, body="decomposed body")
    return f"{_DIRECTORY_REL}/{_COMPOSED_NAME}", directory


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in directory.iterdir()
        if unicodedata.normalize("NFKC", path.name) == _COMPOSED_NAME
    }


def test_withheld_nfd_collision_reads_as_missing_to_restricted_caller(vault: Path) -> None:
    """A collision on a withheld path must not answer AMBIGUOUS_PATH: that
    would tell a denied caller the page exists in two spellings."""
    rel, directory = _collision(vault)
    _govern_deny(vault)

    with request_scope(_external()), pytest.raises(ValueError) as withheld:
        commands.op_get(vault, path=rel)

    (directory / _COMPOSED_NAME).unlink()
    (directory / _DECOMPOSED_NAME).unlink()
    with request_scope(_external()), pytest.raises(ValueError) as absent:
        commands.op_get(vault, path=rel)

    assert str(withheld.value) == str(absent.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"new_body": _page_text("edited body").split("---\n\n", 1)[1]},
        {"new_body": _page_text("edited body").split("---\n\n", 1)[1], "validate_only": True},
        {"new_body": ""},
    ],
    ids=["commit", "validate-only", "semantic-refusal"],
)
def test_edit_never_renames_an_nfd_page_while_resolving_it(vault: Path, kwargs: dict) -> None:
    """Resolving a path is not a write: no dry run, refused edit, or
    unauthorized caller may leave the page renamed behind it."""
    rel = _nfd_page(vault)
    directory = vault.joinpath(*_DIRECTORY)
    before = _snapshot(directory)

    with pytest.raises(edit_module.EditError) as refused:
        edit_module.edit(vault, path=rel, why="NFD edit probe", today=TODAY, **kwargs)

    assert refused.value.code == "NON_CANONICAL_NAME"
    assert "move_file" in refused.value.reason
    assert _snapshot(directory) == before


def test_edit_by_a_restricted_caller_never_renames_a_withheld_nfd_page(vault: Path) -> None:
    rel = _nfd_page(vault)
    _govern_deny(vault)
    directory = vault.joinpath(*_DIRECTORY)
    before = _snapshot(directory)

    with request_scope(_external()), pytest.raises(edit_module.EditError) as withheld:
        edit_module.edit(vault, path=rel, why="probe", new_body="\nbody\n", today=TODAY)
    assert _snapshot(directory) == before

    (directory / _DECOMPOSED_NAME).unlink()
    with request_scope(_external()), pytest.raises(edit_module.EditError) as absent:
        edit_module.edit(vault, path=rel, why="probe", new_body="\nbody\n", today=TODAY)
    assert withheld.value.as_dict() == absent.value.as_dict()


@pytest.mark.parametrize("validate_only", [False, True], ids=["commit", "validate-only"])
def test_replace_never_renames_an_nfd_page_while_resolving_it(
    vault: Path, validate_only: bool
) -> None:
    rel = _nfd_page(vault)
    directory = vault.joinpath(*_DIRECTORY)
    before = _snapshot(directory)

    with pytest.raises(replace_module.ReplaceError) as refused:
        replace_module.replace(
            vault,
            old_path=rel,
            content="\n# NFD replace successor\n\n## Claim\n\nrevised\n",
            note_type="insight",
            title="NFD replace successor",
            today=TODAY,
            validate_only=validate_only,
        )

    assert refused.value.code == "NON_CANONICAL_NAME"
    assert _snapshot(directory) == before


def test_move_file_onto_its_own_name_canonicalizes_an_nfd_page(vault: Path) -> None:
    """The governed way to fix an NFD name that edit and replace refuse."""
    rel = _nfd_page(vault)
    directory = vault.joinpath(*_DIRECTORY)

    move_module.move_file(vault, old_path=rel, new_path=rel, today=TODAY, update_wikilinks=False)

    assert not (directory / _DECOMPOSED_NAME).exists()
    assert "probe body" in (directory / _COMPOSED_NAME).read_text(encoding="utf-8")
    edit_module.edit(
        vault,
        path=rel,
        why="edit after canonicalizing",
        new_body=_page_text("edited body").split("---\n\n", 1)[1],
        today=TODAY,
    )
    assert "edited body" in commands.op_get(vault, path=rel)["body"]


def test_edit_refuses_a_collision_even_when_the_nfkc_spelling_exists(vault: Path) -> None:
    rel, directory = _collision(vault)
    before = _snapshot(directory)

    with pytest.raises(edit_module.EditError) as refused:
        edit_module.edit(
            vault,
            path=rel,
            why="collision probe",
            new_body=_page_text("edited body").split("---\n\n", 1)[1],
            today=TODAY,
        )

    assert refused.value.code == "AMBIGUOUS_PATH"
    assert _snapshot(directory) == before


def test_replace_refuses_a_collision_even_when_the_nfkc_spelling_exists(vault: Path) -> None:
    rel, directory = _collision(vault)
    before = _snapshot(directory)

    with pytest.raises(replace_module.ReplaceError) as refused:
        replace_module.replace(
            vault,
            old_path=rel,
            content="\n# Collision successor\n\n## Claim\n\nrevised\n",
            note_type="insight",
            title="Collision successor",
            today=TODAY,
        )

    assert refused.value.code == "AMBIGUOUS_PATH"
    assert _snapshot(directory) == before


def test_move_file_refuses_a_collision_even_when_the_nfkc_spelling_exists(vault: Path) -> None:
    rel, directory = _collision(vault)
    before = _snapshot(directory)

    with pytest.raises(move_module.MoveFileError) as refused:
        move_module.move_file(
            vault,
            old_path=rel,
            new_path=f"{_DIRECTORY_REL}/moved-collision.md",
            today=TODAY,
            update_wikilinks=False,
        )

    assert refused.value.code == "AMBIGUOUS_PATH"
    assert _snapshot(directory) == before
    assert not (vault / _DIRECTORY_REL / "moved-collision.md").exists()


def test_withheld_collision_is_absent_to_a_restricted_write_caller(vault: Path) -> None:
    rel, directory = _collision(vault)
    _govern_deny(vault)
    new_rel = f"{_DIRECTORY_REL}/moved-collision.md"

    def attempts() -> list[dict]:
        outcomes = []
        with request_scope(_external()):
            with pytest.raises(edit_module.EditError) as edited:
                edit_module.edit(vault, path=rel, why="probe", new_body="\nbody\n", today=TODAY)
            outcomes.append(edited.value.as_dict())
            with pytest.raises(move_module.MoveFileError) as moved:
                move_module.move_file(
                    vault, old_path=rel, new_path=new_rel, today=TODAY, update_wikilinks=False
                )
            outcomes.append({"code": moved.value.code, "reason": moved.value.reason})
        return outcomes

    withheld = attempts()
    (directory / _COMPOSED_NAME).unlink()
    (directory / _DECOMPOSED_NAME).unlink()
    assert withheld == attempts()
