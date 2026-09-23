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


# ---------------------------------------------------------------------------
# Connect context and graph-context decide what they assemble
# ---------------------------------------------------------------------------


def _inbound_linker() -> tuple[dict[str, str], dict[str, str]]:
    """A withheld page mentions visible pages and contradicts one of them."""
    base = {
        **_filler(),
        f"{NOTES}/alpha.md": _page("Alpha", "Alpha rollout notes.", type="insight"),
        f"{NOTES}/beta.md": _page("Beta", "Beta rollout background.", type="insight"),
    }
    withheld = {
        f"{WITHHELD_DIR}/linker.md": _page(
            "Hidden Draft",
            f"Mentions [[{NOTES}/beta]] and [[{NOTES}/lonely]].\n\n## Relations\n\n"
            f"- supports [[{NOTES}/beta]]\n- contradicts [[{NOTES}/alpha]]\n",
            type="insight",
        )
    }
    return base, withheld


_CONTEXT_SURFACES: dict[str, dict[str, Any]] = {
    "graph-context-query": {"operation": "graph-context", "query": "beta"},
    "context-query": {"operation": "context", "query": "beta"},
    "graph-context-alpha-depth-2": {
        "operation": "graph-context",
        "path": f"{NOTES}/alpha.md",
        "depth": 2,
    },
    "context-beta": {"operation": "context", "path": f"{NOTES}/beta.md"},
    "context-withheld-path": {"operation": "context", "path": f"{WITHHELD_DIR}/linker.md"},
}


@pytest.mark.parametrize("audience", AUDIENCES)
def test_restricted_context_reads_as_if_the_withheld_page_were_absent(
    tmp_path: Path, audience: str
) -> None:
    base, withheld = _inbound_linker()
    vaults = _twins(tmp_path, base, withheld, audience)
    principal = _principal(audience)

    answers = {
        variant: {
            label: _call(vault, principal, "connect_memory", **kwargs)
            for label, kwargs in _CONTEXT_SURFACES.items()
        }
        for variant, vault in vaults.items()
    }

    for label in _CONTEXT_SURFACES:
        assert _text(answers["A"][label]) == _text(answers["B"][label]), label
        assert _text(answers["C"][label]) == _text(answers["B"][label]), label
    assert answers["A"]["context-withheld-path"]["message"].startswith("NOT_FOUND")
    # The owner still receives the withheld page as a neighbour of beta.
    owner = _call(vaults["A"], None, "connect_memory", operation="context", path=f"{NOTES}/beta.md")
    assert f"{WITHHELD_DIR}/linker.md" in _text(owner)


# ---------------------------------------------------------------------------
# Relation proposals decide each target before emitting it
# ---------------------------------------------------------------------------

_SOURCE = f"{KB}/Sources/source-one"


def _shared_source() -> tuple[dict[str, str], dict[str, str]]:
    """A withheld page cites the same source as a visible page."""
    base = {
        **_filler(),
        f"{_SOURCE}.md": _page("Source One", "Raw source text.", type="source"),
        f"{NOTES}/alpha.md": _page(
            "Alpha", "Alpha conclusions.", type="insight", sources=f'["[[{_SOURCE}]]"]'
        ),
        f"{NOTES}/beta.md": _page("Beta", "Beta background.", type="insight"),
    }
    withheld = {
        f"{WITHHELD_DIR}/merger-memo.md": _page(
            "Hidden Draft", "Withheld body text.", type="insight", sources=f'["[[{_SOURCE}]]"]'
        )
    }
    return base, withheld


_QUEUE_FIELDS = ("groups", "shown", "pages_shown", "filtered", "items_truncated", "status")


@pytest.mark.parametrize("audience", AUDIENCES)
def test_relation_proposals_read_as_if_the_withheld_page_were_absent(
    tmp_path: Path, audience: str
) -> None:
    base, withheld = _shared_source()
    vaults = _twins(tmp_path, base, withheld, audience)
    principal = _principal(audience)

    answers = {}
    for variant, vault in vaults.items():
        queue = _call(vault, principal, "review_memory", mode="relation-queue")
        answers[variant] = {
            "suggest": _call(
                vault,
                principal,
                "connect_memory",
                operation="suggest-relations",
                path=f"{NOTES}/alpha.md",
            ),
            "queue": {field: queue.get(field) for field in _QUEUE_FIELDS},
        }

    assert not _names_withheld(answers["A"])
    assert _text(answers["A"]) == _text(answers["B"])
    assert _text(answers["C"]) == _text(answers["B"])
    owner = _call(
        vaults["A"], None, "connect_memory", operation="suggest-relations", path=f"{NOTES}/alpha.md"
    )
    assert f"{WITHHELD_DIR}/merger-memo.md" in _text(owner)


@pytest.mark.parametrize("audience", AUDIENCES)
def test_a_guessed_relation_ref_to_a_withheld_page_reads_as_absent(
    tmp_path: Path, audience: str
) -> None:
    from exomem import relation_queue, review_state

    base, withheld = _shared_source()
    vaults = {
        "B": _materialize(tmp_path / "B" / "vault", dict(base), audience),
        "A": _materialize(tmp_path / "A" / "vault", {**base, **withheld}, audience),
    }
    guess = "|".join(
        (
            f"{NOTES}/alpha.md",
            f"{WITHHELD_DIR}/merger-memo.md",
            "relates_to",
            "shared_sources",
        )
    )
    ref = relation_queue.relation_review_ref(review_state.item_id(f"relation:{guess}"))

    answers = {
        variant: _call(
            vault,
            _principal(audience),
            "triage_memory",
            ref=ref,
            action="dismiss",
            source_path=f"{NOTES}/alpha.md",
        )
        for variant, vault in vaults.items()
    }

    assert _text(answers["A"]) == _text(answers["B"])
    assert answers["B"]["message"].startswith("REVIEW_REFRESH_REQUIRED")


# ---------------------------------------------------------------------------
# Evolution timelines are built over visible pages only
# ---------------------------------------------------------------------------


def _visible_pointer() -> tuple[dict[str, str], dict[str, str]]:
    """A visible page names its withheld successor; the successor names it back."""
    base = {
        **_filler(),
        f"{NOTES}/beta.md": _page(
            "Beta",
            "Beta rollout background.",
            type="insight",
            status="superseded",
            superseded_by=f'["[[{WITHHELD_DIR}/newer]]"]',
        ),
    }
    withheld = {
        f"{WITHHELD_DIR}/newer.md": _page(
            "Hidden Draft",
            "Withheld body text about beta rollout.",
            type="insight",
            supersedes=f'["[[{NOTES}/beta]]"]',
        )
    }
    return base, withheld


_EVOLUTION_SURFACES: dict[str, dict[str, Any]] = {
    "query": {"mode": "evolution", "query": "beta rollout"},
    "path": {"mode": "evolution", "path": f"{NOTES}/beta.md"},
    "withheld-path": {"mode": "evolution", "path": f"{WITHHELD_DIR}/newer.md"},
}


@pytest.mark.parametrize("scenario", ["supersedes-visible", "visible-pointer"])
@pytest.mark.parametrize("audience", AUDIENCES)
def test_restricted_evolution_reads_as_if_the_withheld_page_were_absent(
    tmp_path: Path, audience: str, scenario: str
) -> None:
    base, withheld = _supersede() if scenario == "supersedes-visible" else _visible_pointer()
    vaults = _twins(tmp_path, base, withheld, audience)
    principal = _principal(audience)

    answers = {
        variant: {
            label: _call(vault, principal, "review_memory", **kwargs)
            for label, kwargs in _EVOLUTION_SURFACES.items()
        }
        for variant, vault in vaults.items()
    }

    # A refusal echoes the caller's own spelling of the path it asked for.
    assert not _names_withheld({k: v for k, v in answers["A"].items() if k != "withheld-path"})
    for label in _EVOLUTION_SURFACES:
        assert _text(answers["A"][label]) == _text(answers["B"][label]), label
        assert _text(answers["C"][label]) == _text(answers["B"][label]), label
    owner = _call(vaults["A"], None, "review_memory", mode="evolution", query="beta rollout")
    assert f"{WITHHELD_DIR}/newer.md" in _text(owner["timelines"]), owner


# ---------------------------------------------------------------------------
# A restricted writer's links resolve over the pages it may see
# ---------------------------------------------------------------------------

_GUESSES = (
    "Probe page. Guesses: [[beta]] [[project-zeta-plan]] [[Hidden Plan]] "
    f"[[{WITHHELD_DIR}/project-zeta-plan]] [[nonexistent-guess]] [[other]].\n\n"
    "## Observations\n\n- [operating constraint] Keep retries bounded #reliability\n"
)


def _writer_fixture() -> tuple[dict[str, str], dict[str, str]]:
    base = {
        f"{NOTES}/beta.md": _page("Beta", "Beta background.", type="insight"),
        f"{NOTES}/other.md": _page("Other", "Other background.", type="insight"),
    }
    withheld = {
        f"{WITHHELD_DIR}/beta.md": _page("Hidden Draft", "Withheld body text.", type="insight"),
        f"{WITHHELD_DIR}/project-zeta-plan.md": _page(
            "Hidden Plan", "Withheld body text.", type="insight", title="Hidden Plan"
        ),
    }
    return base, withheld


def _written(vault: Path, principal: RequestPrincipal | None) -> dict[str, Any]:
    from exomem import capture_sweep

    capture_sweep.reset_state()
    answer = _call(vault, principal, "remember", content=_GUESSES, title="Probe Page", note_type="insight")
    assert "__error__" not in answer, answer
    body = (vault / answer["path"]).read_text(encoding="utf-8").split("\n---\n", 1)[1]
    sweep = answer.get("capture_sweep") or {}
    return {
        "path": answer["path"],
        "warnings": answer.get("warnings"),
        "unpaged_mentions": sweep.get("unpaged_mentions"),
        "body": body,
    }


@pytest.mark.parametrize("audience", AUDIENCES)
def test_a_restricted_writer_resolves_links_as_if_the_withheld_page_were_absent(
    tmp_path: Path, audience: str
) -> None:
    base, withheld = _writer_fixture()
    vaults = _twins(tmp_path, base, withheld, audience)

    written = {variant: _written(vault, _principal(audience)) for variant, vault in vaults.items()}

    # The writer's own guesses stay as it wrote them; none is rewritten onto a
    # withheld page, and the warnings and unpaged mentions match the twin.
    assert _text(written["A"]) == _text(written["B"])
    assert _text(written["C"]) == _text(written["B"])
    assert "[[Knowledge Base/Notes/beta]]" in written["B"]["body"]


def test_the_owner_writer_still_resolves_over_every_page(tmp_path: Path) -> None:
    base, withheld = _writer_fixture()
    vault = _materialize(tmp_path / "vault", {**base, **withheld}, "external")

    written = _written(vault, None)

    assert "Hidden Plan" not in written["body"]
    assert f"[[{WITHHELD_DIR}/project-zeta-plan]]" in written["body"]
    assert any(f"{WITHHELD_DIR}/beta" in warning for warning in written["warnings"])


# ---------------------------------------------------------------------------
# Entity identity is decided before it is reported
# ---------------------------------------------------------------------------

PEOPLE = f"{KB}/Entities/People"


def _entities() -> tuple[dict[str, str], dict[str, str]]:
    base = {
        f"{PEOPLE}/other-person.md": _page(
            "Other Person", "A visible person.", type="entity", entity_type="person",
            status="active", title="Other Person",
        ),
        f"{NOTES}/meeting.md": _page("Meeting", "Met with Other Person.", type="insight"),
    }
    withheld = {
        f"{PEOPLE}/private-dana-example.md": _page(
            "Dana Example", "Withheld body text.", type="entity", entity_type="person",
            status="active", title="Dana Example", aliases='["D. Example"]',
        )
    }
    return base, withheld


def _entity_view(answer: Any) -> Any:
    if not isinstance(answer, dict) or "__error__" in answer:
        return answer
    return {key: answer.get(key) for key in ("status", "candidates", "omitted_candidate_count", "path", "code")}


@pytest.mark.parametrize("audience", AUDIENCES)
def test_restricted_entity_identity_reads_as_if_the_withheld_entity_were_absent(
    tmp_path: Path, audience: str
) -> None:
    base, withheld = _entities()
    vaults = {
        variant: _materialize(
            tmp_path / variant / "vault",
            {**base, **(withheld if variant == "A" else {})},
            audience,
            scope="Entities/People/private-*",
        )
        for variant in ("B", "A")
    }
    principal = _principal(audience)

    answers = {}
    for variant, vault in vaults.items():
        answers[variant] = {
            name: _entity_view(
                _call(vault, principal, "connect_memory", operation="resolve-entity", name=name)
            )
            for name in ("Dana Example", "DANA example", "D. Example", "Nobody Here")
        }
        answers[variant]["create"] = _entity_view(
            _call(
                vault,
                principal,
                "connect_memory",
                operation="create-entity",
                entity_type="person",
                name="D. Example",
                summary="A person met once.",
            )
        )

    assert "Dana" not in _text(list(answers["A"].values()))
    assert _text(answers["A"]) == _text(answers["B"])
    assert answers["A"]["Dana Example"]["status"] == "no_match"


# ---------------------------------------------------------------------------
# Directory listings collapse what the caller may not see
# ---------------------------------------------------------------------------


_BROWSE_SURFACES: dict[str, dict[str, Any]] = {
    "list-notes": {"mode": "list", "path": NOTES},
    "list-notes-recursive": {"mode": "list", "path": NOTES, "recursive": True},
    "list-withheld-folder": {"mode": "list", "path": WITHHELD_DIR},
    "list-withheld-file": {"mode": "list", "path": f"{WITHHELD_DIR}/linker.md"},
    "overview": {"mode": "overview"},
    "overview-notes": {"mode": "overview", "path": NOTES},
}


@pytest.mark.parametrize("audience", AUDIENCES)
def test_restricted_browsing_reads_as_if_the_withheld_folder_were_absent(
    tmp_path: Path, audience: str
) -> None:
    base, withheld = _inbound_linker()
    vaults = {
        "B": _materialize(tmp_path / "B" / "vault", dict(base), audience),
        "A": _materialize(tmp_path / "A" / "vault", {**base, **withheld}, audience),
    }
    principal = _principal(audience)

    answers = {
        variant: {
            label: _call(vault, principal, "browse_memory", **kwargs)
            for label, kwargs in _BROWSE_SURFACES.items()
        }
        for variant, vault in vaults.items()
    }

    # A refusal echoes the caller's own spelling of the path it asked for.
    assert not _names_withheld(
        {k: v for k, v in answers["A"].items() if not k.startswith("list-withheld")}
    )
    for label in _BROWSE_SURFACES:
        assert _text(answers["A"][label]) == _text(answers["B"][label]), label
    owner = _call(vaults["A"], None, "browse_memory", mode="list", path=NOTES)
    assert WITHHELD_DIR in _text(owner)
