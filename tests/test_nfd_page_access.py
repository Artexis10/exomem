"""Single-page read/write doors open pages named in Unicode NFD form.

A vault synced from macOS keeps decomposed (NFD) file names on a byte-exact
file system (Linux ext4): `cafe-e-nfd.md` on disk is not the same name as its
NFKC (composed) spelling. `find_corpus`'s whole-vault walk already handles
this (it opens the spelling it found, via `physical=True`), but the
single-path doors -- `get`/`read_memory`, `edit`/`edit_memory`,
`replace`/`replace_memory`, and `move_file`'s source -- opened only the NFKC
spelling and reported the page missing. See `reserved_paths.resolve_physical_relative`.
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


# ---------------- readable + editable through op_* ----------------


def test_nfd_named_page_is_readable_via_op_get(vault: Path) -> None:
    rel = _nfd_page(vault)
    out = commands.op_get(vault, path=rel)
    assert "probe body" in out["body"]
    assert out["frontmatter"]["type"] == "insight"


def test_nfd_named_page_is_readable_via_get_frontmatter(vault: Path) -> None:
    rel = _nfd_page(vault)
    out = commands.op_get(vault, path=rel, frontmatter_only=True)
    assert out["frontmatter"]["type"] == "insight"


def test_nfd_named_page_is_editable_via_edit(vault: Path) -> None:
    rel = _nfd_page(vault)
    directory = vault.joinpath(*_DIRECTORY)

    edit_module.edit(
        vault,
        path=rel,
        why="NFD edit probe",
        new_body=_page_text("edited body").split("---\n\n", 1)[1],
        today=TODAY,
    )

    # A write canonicalizes the on-disk name to its NFKC spelling as part of
    # the edit (every downstream write-side invariant -- the semantic index,
    # the graph checkpoint -- keys on that canonical form), so the decomposed
    # physical name is gone and the composed one now holds the edited content.
    assert not (directory / _DECOMPOSED_NAME).exists()
    on_disk = directory / _COMPOSED_NAME
    assert on_disk.exists()
    assert "edited body" in on_disk.read_text(encoding="utf-8")

    out = commands.op_get(vault, path=rel)
    assert "edited body" in out["body"]


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


def test_nfd_named_page_is_replaceable_via_replace(vault: Path) -> None:
    old_rel = _nfd_page(vault)
    directory = vault.joinpath(*_DIRECTORY)
    old_abs = directory / _DECOMPOSED_NAME
    assert old_abs.exists()

    result = replace_module.replace(
        vault,
        old_path=old_rel,
        content=_page_text("revised body").split("---\n\n", 1)[1].replace(
            "NFD probe", "NFD replace successor"
        ),
        note_type="insight",
        title="NFD replace successor",
        today=TODAY,
    )

    # The old page stays (never deleted), now marked superseded -- and, like
    # `edit`, canonicalized to its NFKC spelling as part of the write.
    canonical_abs = directory / _COMPOSED_NAME
    assert not old_abs.exists()
    assert canonical_abs.exists()
    assert "superseded" in canonical_abs.read_text(encoding="utf-8")
    assert (vault / result.new_path).exists()


# ---------------- readable + editable through the REST door ----------------


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


def test_nfd_named_page_is_editable_via_rest_edit_memory(
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
    assert r.status_code == 200, r.text

    directory = vault.joinpath(*_DIRECTORY)
    assert not (directory / _DECOMPOSED_NAME).exists()
    assert "rest edited" in (directory / _COMPOSED_NAME).read_text(encoding="utf-8")


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
