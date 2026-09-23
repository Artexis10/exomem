"""Derived identifiers are decided before a restricted caller receives them.

The release plane decides a page when a surface names it in an entry field it
knows about. Several derived structures carry a page identifier in fields the
plane did not inspect (`to`, `from`, graph node keys, pair members, timeline
anchors) or computed their answer over the whole vault before any decision.
These tests build twin vaults and ask the same questions of each as a
restricted caller:

- **B** holds no withheld page;
- **A** holds one withheld page that collides with, or links to, visible pages;
- **C** holds one withheld page that touches nothing visible.

The restricted answers must not name a withheld page, and where the withheld
page cannot change what the caller may see, the answers must be identical.
Each case runs for the `external` audience and for a verified principal, since
a rule names the audience it restricts exactly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from exomem import commands, epistemic_graph, writer_lease
from exomem import find as find_module
from exomem.governance import egress
from exomem.governance.principal import (
    RequestPrincipal,
    library_scope,
    owner_principal,
    request_scope,
)

KB = "Knowledge Base"
NOTES = f"{KB}/Notes"
WITHHELD_DIR = f"{NOTES}/Withheld"
SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
FIXED_MTIME = 1_780_000_000
PRINCIPAL_AUDIENCE = "principal:" + "ab" * 32
AUDIENCES = ("external", PRINCIPAL_AUDIENCE)
_COMMANDS = {command.name: command for command in commands.PRODUCT_COMMANDS}


def _page(h1: str, body: str, **frontmatter: Any) -> str:
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {value if isinstance(value, str) else json.dumps(value)}")
    lines += ["---", f"# {h1}", "", body, ""]
    return "\n".join(lines)


def _filler() -> dict[str, str]:
    files = {
        f"{NOTES}/filler-{word}.md": _page(
            f"Filler {word.title()}", f"A note about {word} logistics {index}.", type="insight"
        )
        for index, word in enumerate(("orchard", "harbor", "lantern", "meadow"))
    }
    files[f"{NOTES}/lonely.md"] = _page("Lonely", "A page nobody links.", type="insight")
    return files


def _govern(vault: Path, audience: str, scope: str = "Notes/Withheld/**") -> None:
    governance = vault / KB / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "withheld.yaml").write_text(
        f'governance_version: 1\nid: {SCOPE_ID}\nname: Withheld\npaths: ["{scope}"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "withheld.yaml").write_text(
        f'governance_version: 1\nid: {RULE_ID}\nscope_ids: ["{SCOPE_ID}"]\n'
        f"audience: {audience}\nceiling: {egress.LEVEL_NONE}\n",
        encoding="utf-8",
    )


def _reset() -> None:
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    policy._LAST_GOOD.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()


def _materialize(
    vault: Path, files: dict[str, str], audience: str, scope: str = "Notes/Withheld/**"
) -> Path:
    for rel, text in files.items():
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _govern(vault, audience, scope)
    for dirpath, _dirnames, filenames in os.walk(vault):
        for name in filenames:
            os.utime(os.path.join(dirpath, name), (FIXED_MTIME, FIXED_MTIME))
    _reset()
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    _reset()
    return vault


def _twins(
    tmp_path: Path,
    base: dict[str, str],
    withheld: dict[str, str],
    audience: str,
) -> dict[str, Path]:
    """Build B (no withheld page), A (`withheld`) and C (a neutral withheld page)."""
    neutral = {
        f"{WITHHELD_DIR}/unrelated-draft.md": _page(
            "Unrelated Draft", "Withheld body text.", type="insight"
        )
    }
    return {
        "B": _materialize(tmp_path / "B" / "vault", dict(base), audience),
        "A": _materialize(tmp_path / "A" / "vault", {**base, **withheld}, audience),
        "C": _materialize(tmp_path / "C" / "vault", {**base, **neutral}, audience),
    }


def _principal(audience: str) -> RequestPrincipal:
    return RequestPrincipal(audience_id=audience, surface="mcp", resolved=True)


def _call(vault: Path, principal: RequestPrincipal | None, command: str, **kwargs: Any) -> Any:
    _reset()
    try:
        if principal is None:
            with library_scope():
                result = writer_lease.invoke_command(_COMMANDS[command], vault, **kwargs)
        else:
            with request_scope(principal):
                result = writer_lease.invoke_command(_COMMANDS[command], vault, **kwargs)
    except Exception as error:  # noqa: BLE001 - the error text is part of the answer
        result = {"__error__": type(error).__name__, "message": str(error)}
    text = json.dumps(result, sort_keys=True, default=str).replace(str(vault), "<vault>")
    return json.loads(text)


def _text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _names_withheld(value: Any) -> bool:
    text = _text(value)
    return "Withheld" in text or "Hidden Draft" in text


# ---------------------------------------------------------------------------
# Identifier fields the backstop decides
# ---------------------------------------------------------------------------


@pytest.fixture
def governed(tmp_path: Path) -> Path:
    files = {
        f"{NOTES}/open.md": _page("Open", "Visible text.", type="insight"),
        f"{WITHHELD_DIR}/secret.md": _page("Secret", "Withheld text.", type="insight"),
    }
    return _materialize(tmp_path / "vault", files, "external")


_SECRET = f"{WITHHELD_DIR}/secret.md"
_OPEN = f"{NOTES}/open.md"


@pytest.mark.parametrize(
    ("field", "secret", "visible"),
    [
        ("to", _SECRET, _OPEN),
        ("from", _SECRET, _OPEN),
        ("a", _SECRET, _OPEN),
        ("b", _SECRET, _OPEN),
        ("topic_anchor", _SECRET, _OPEN),
        ("chain_id", _SECRET, _OPEN),
        ("src_key", f"file:{_SECRET}", f"file:{_OPEN}"),
        ("dst_key", f"file:{_SECRET}", f"file:{_OPEN}"),
        # An identifier-shaped field the list does not name is decided too.
        ("shared_source", _SECRET, _OPEN),
        ("anchor_path", _SECRET, _OPEN),
        ("seed_key", f"file:{_SECRET}", f"file:{_OPEN}"),
        # A reference field may carry the path without its extension, or as a
        # vault URI.
        ("to", _SECRET.removesuffix(".md"), _OPEN.removesuffix(".md")),
        ("target_ref", "exomem://vault/" + _SECRET.replace(" ", "%20"), "exomem://vault/" + _OPEN),
    ],
)
def test_an_identifier_field_naming_a_withheld_page_drops_its_entry(
    governed: Path, field: str, secret: str, visible: str
) -> None:
    payload = {"items": [{field: secret, "n": 1}, {field: visible, "n": 2}]}

    out = egress.filter_withheld_entries(
        governed, payload, principal=_principal("external")
    )

    assert out == {"items": [{field: visible, "n": 2}]}


def test_a_bullet_linking_a_withheld_page_drops_its_entry(governed: Path) -> None:
    payload = {
        "items": [
            {"bullet": f"- relates_to [[{_SECRET.removesuffix('.md')}]]", "n": 1},
            {"bullet": f"- relates_to [[{_OPEN.removesuffix('.md')}|open]]", "n": 2},
        ]
    }

    out = egress.filter_withheld_entries(
        governed, payload, principal=_principal("external")
    )

    assert out == {"items": [payload["items"][1]]}


def test_non_path_values_in_identifier_fields_are_kept(governed: Path) -> None:
    payload = {
        "items": [
            {"from": "open", "to": "closed", "a": 0.5, "b": None, "chain_id": "chain-1"},
            {"src_key": "block:0123456789abcdef", "dst_key": "unit:abc", "edge_key": "edge:1"},
        ]
    }

    out = egress.filter_withheld_entries(
        governed, payload, principal=_principal("external")
    )

    assert out == payload


def test_the_owner_keeps_every_identifier(governed: Path) -> None:
    payload = {"items": [{"to": _SECRET}, {"dst_key": f"file:{_SECRET}"}]}

    out = egress.filter_withheld_entries(
        governed, payload, principal=owner_principal(surface="mcp")
    )

    assert out == payload


# ---------------------------------------------------------------------------
# End to end: no restricted derived surface names the withheld page
# ---------------------------------------------------------------------------


def _stem_beats_title() -> tuple[dict[str, str], dict[str, str]]:
    """A visible title `gamma` loses to a withheld stem `gamma`."""
    base = {
        **_filler(),
        f"{NOTES}/alpha.md": _page(
            "Alpha",
            "See [[gamma]] for background on the rollout.\n\n## Relations\n\n- supports [[gamma]]\n",
            type="insight",
        ),
        f"{NOTES}/g-page.md": _page("gamma", "Gamma rollout background.", type="insight", title="gamma"),
    }
    withheld = {f"{WITHHELD_DIR}/gamma.md": _page("Hidden Draft", "Withheld body text.", type="insight")}
    return base, withheld


def _supersede() -> tuple[dict[str, str], dict[str, str]]:
    """A withheld page supersedes a visible one."""
    base = {
        **_filler(),
        f"{NOTES}/alpha.md": _page("Alpha", "See [[beta]].", type="insight"),
        f"{NOTES}/beta.md": _page("Beta", "Beta rollout background.", type="insight"),
    }
    withheld = {
        f"{WITHHELD_DIR}/newer.md": _page(
            "Hidden Draft",
            "Withheld body text about beta rollout.",
            type="insight",
            supersedes='"[[Knowledge Base/Notes/beta]]"',
        )
    }
    return base, withheld


_DERIVED_SURFACES: dict[str, tuple[str, dict[str, Any]]] = {
    "suggest-relations": (
        "connect_memory",
        {"operation": "suggest-relations", "path": f"{NOTES}/alpha.md"},
    ),
    "graph-context": ("connect_memory", {"operation": "graph-context", "path": f"{NOTES}/alpha.md"}),
    "context": ("connect_memory", {"operation": "context", "path": f"{NOTES}/alpha.md"}),
    "graph-context-query": ("connect_memory", {"operation": "graph-context", "query": "gamma"}),
    "relation-queue": ("review_memory", {"mode": "relation-queue"}),
}


@pytest.mark.parametrize("audience", AUDIENCES)
def test_no_restricted_derived_surface_names_a_colliding_withheld_page(
    tmp_path: Path, audience: str
) -> None:
    base, withheld = _stem_beats_title()
    vault = _materialize(tmp_path / "vault", {**base, **withheld}, audience)
    principal = _principal(audience)

    for label, (command, kwargs) in _DERIVED_SURFACES.items():
        answer = _call(vault, principal, command, **kwargs)
        assert "__error__" not in answer, (label, answer)
        assert not _names_withheld(answer), (label, answer)

    # The owner still sees the page the link resolves to.
    owner = _call(vault, None, "connect_memory", operation="suggest-relations", path=f"{NOTES}/alpha.md")
    assert f"{WITHHELD_DIR}/gamma.md" in _text(owner)


@pytest.mark.parametrize("audience", AUDIENCES)
def test_no_restricted_timeline_is_anchored_on_a_withheld_page(
    tmp_path: Path, audience: str
) -> None:
    base, withheld = _supersede()
    vault = _materialize(tmp_path / "vault", {**base, **withheld}, audience)

    answer = _call(vault, _principal(audience), "review_memory", mode="evolution", query="beta")

    assert "__error__" not in answer, answer
    assert not _names_withheld(answer), answer
