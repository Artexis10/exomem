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
import re
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
    """Withhold `scope` (one glob, or several separated by commas) at L0."""
    governance = vault / KB / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    paths = json.dumps([glob.strip() for glob in scope.split(",")])
    (governance / "scopes" / "withheld.yaml").write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Withheld\npaths: {paths}\n",
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


# ---------------------------------------------------------------------------
# Write doors decide their target before resolving or mutating it
# ---------------------------------------------------------------------------

_WITHHELD_TARGET = f"{WITHHELD_DIR}/target.md"
_WITHHELD_SOURCE = f"{KB}/Sources/Withheld/private-source.md"
_WRITE_SCOPE = "Notes/Withheld/**, Sources/Withheld/**"


def _write_door_fixture() -> tuple[dict[str, str], dict[str, str]]:
    base = {
        f"{NOTES}/alpha.md": _page("Alpha", "Alpha conclusions.", type="insight"),
        f"{KB}/Sources/open-source.md": _page("Open Source", "Raw text.", type="source"),
    }
    withheld = {
        _WITHHELD_TARGET: _page(
            "Hidden Draft",
            "Withheld body text.\n\n## Observations\n\n- [finding] A withheld finding\n",
            type="insight",
            exomem_id="0192f0a4-6b7c-4d8e-9f10-a1b2c3d4e5f6",
        ),
        _WITHHELD_SOURCE: _page(
            "Hidden Source", "Withheld raw text.", type="source", ingested_into="[]"
        ),
    }
    return base, withheld


_WRITE_DOORS: dict[str, tuple[str, dict[str, Any]]] = {
    "edit-replace-string": (
        "edit_memory",
        {
            "path": _WITHHELD_TARGET,
            "why": "fix",
            "operation": {"kind": "replace_string", "old_string": "Withheld", "new_string": "X"},
        },
    ),
    "edit-bare-path": (
        "edit_memory",
        {
            "path": "Notes/Withheld/target",
            "why": "fix",
            "operation": {"kind": "replace_tags", "tags": ["x"]},
        },
    ),
    "observe-add": (
        "observe_memory",
        {"path": _WITHHELD_TARGET, "operation": "add", "category": "finding", "content": "New."},
    ),
    "replace": (
        "replace_memory",
        {
            "old_path": _WITHHELD_TARGET,
            "content": "Replacement.\n\n## Observations\n\n- [finding] Replaced\n",
            "title": "Replacement Page",
            "reason": "supersede",
        },
    ),
    "append": (
        "manage_memory_file",
        {"operation": "append", "path": _WITHHELD_TARGET, "content": "More text."},
    ),
    "move": (
        "manage_memory_file",
        {"operation": "move", "old_path": _WITHHELD_TARGET, "new_path": f"{NOTES}/moved.md"},
    ),
    "delete": (
        "manage_memory_file",
        {"operation": "delete", "path": _WITHHELD_TARGET, "confirm": True},
    ),
    "delete-folder": (
        "manage_memory_file",
        {"operation": "delete", "path": WITHHELD_DIR, "confirm": True, "recursive": True},
    ),
    "observe-by-reference": (
        "observe_memory",
        {
            "path": "exomem://memory/0192f0a4-6b7c-4d8e-9f10-a1b2c3d4e5f6",
            "operation": "add",
            "category": "finding",
            "content": "New.",
        },
    ),
    "reclassify": (
        "manage_memory_file",
        {
            "operation": "reclassify",
            "path": _WITHHELD_SOURCE,
            "source_kind": "article",
            "reason": "correct",
        },
    ),
    "remember-cites-withheld-source": (
        "remember",
        {
            "content": "A cited note.\n\n## Observations\n\n- [finding] Cited\n",
            "title": "Cited Note",
            "note_type": "insight",
            "sources": [f"[[{_WITHHELD_SOURCE.removesuffix('.md')}]]"],
        },
    ),
}


@pytest.mark.parametrize("door", sorted(_WRITE_DOORS))
@pytest.mark.parametrize("audience", AUDIENCES)
def test_a_write_door_answers_a_withheld_target_as_an_absent_one(
    tmp_path: Path, audience: str, door: str
) -> None:
    base, withheld = _write_door_fixture()
    vaults = {
        "B": _materialize(tmp_path / "B" / "vault", dict(base), audience, scope=_WRITE_SCOPE),
        "A": _materialize(
            tmp_path / "A" / "vault", {**base, **withheld}, audience, scope=_WRITE_SCOPE
        ),
    }
    before = {rel: (vaults["A"] / rel).read_bytes() for rel in withheld}
    command, kwargs = _WRITE_DOORS[door]

    answers = {
        variant: _call(vault, _principal(audience), command, **kwargs)
        for variant, vault in vaults.items()
    }

    assert _text(answers["A"]) == _text(answers["B"])
    assert {rel: (vaults["A"] / rel).read_bytes() for rel in withheld} == before


def test_the_owner_still_writes_to_a_page_withheld_from_others(tmp_path: Path) -> None:
    base, withheld = _write_door_fixture()
    vault = _materialize(tmp_path / "vault", {**base, **withheld}, "external", scope=_WRITE_SCOPE)
    command, kwargs = _WRITE_DOORS["append"]

    answer = _call(vault, None, command, **kwargs)

    assert "__error__" not in answer, answer
    assert "More text." in (vault / _WITHHELD_TARGET).read_text(encoding="utf-8")


def test_a_call_no_surface_bound_keeps_the_write_it_had(tmp_path: Path) -> None:
    """An in-process call outside any request has no audience to decide for.

    The derived and write-door filters apply to a bound caller other than the
    owner; with nothing bound they stand aside, and the dispatcher's entry
    filter still decides what such a call may read.
    """
    base, withheld = _write_door_fixture()
    vault = _materialize(tmp_path / "vault", {**base, **withheld}, "external", scope=_WRITE_SCOPE)
    command, kwargs = _WRITE_DOORS["append"]
    _reset()

    assert egress.restricted_release_filter(vault) is None
    assert egress.write_target_withheld(vault, _WITHHELD_TARGET) is False
    writer_lease.invoke_command(_COMMANDS[command], vault, **kwargs)

    assert "More text." in (vault / _WITHHELD_TARGET).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Counts and ranks follow the filtered list; whole-vault aggregates are the owner's
# ---------------------------------------------------------------------------

_RESTRICTED = {"available": False, "reason": "audience_restricted"}
_VOLATILE_KEYS = frozenset({"first_surfaced_at"})


def _stable(value: Any) -> Any:
    """Drop per-vault timestamps that differ between two otherwise equal runs."""
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


_REVIEW_MODES = ("attention", "activation", "relation-debt", "stale", "contradiction")


@pytest.mark.parametrize("audience", AUDIENCES)
def test_restricted_review_ranks_read_as_if_the_withheld_page_were_absent(
    tmp_path: Path, audience: str
) -> None:
    base, withheld = _inbound_linker()
    vaults = _twins(tmp_path, base, withheld, audience)
    principal = _principal(audience)

    answers = {
        variant: {
            mode: _stable(_call(vault, principal, "review_memory", mode=mode))
            for mode in _REVIEW_MODES
        }
        for variant, vault in vaults.items()
    }

    for mode in _REVIEW_MODES:
        assert "__error__" not in answers["A"][mode], answers["A"][mode]
        assert _text(answers["A"][mode]) == _text(answers["B"][mode]), mode
        assert _text(answers["C"][mode]) == _text(answers["B"][mode]), mode
    ranks = [
        reason["rank"]
        for item in answers["A"]["relation-debt"]["items"]
        for reason in item["reasons"]
    ]
    assert ranks == list(range(1, len(ranks) + 1))
    assert answers["A"]["activation"]["coverage"] == _RESTRICTED
    owner = _call(vaults["A"], None, "review_memory", mode="activation")
    assert owner["coverage"]["eligible_pages"] > 0


_AGGREGATES: dict[str, tuple[str, dict[str, Any]]] = {
    "review-audit": ("review_memory", {"mode": "audit", "detail": "full"}),
    "maintain-audit": ("maintain_memory", {"mode": "audit", "detail": "full"}),
    "infer-relations": ("schema_memory", {"operation": "infer", "subject": "relations"}),
    "infer-categories": ("schema_memory", {"operation": "infer", "subject": "categories"}),
    "diff-relations-corpus": ("schema_memory", {"operation": "diff", "subject": "relations"}),
    "diff-categories-corpus": ("schema_memory", {"operation": "diff", "subject": "categories"}),
}


@pytest.mark.parametrize("audience", AUDIENCES)
def test_whole_vault_aggregates_are_served_to_the_owner_only(
    tmp_path: Path, audience: str
) -> None:
    base, withheld = _inbound_linker()
    vaults = _twins(tmp_path, base, withheld, audience)
    principal = _principal(audience)

    answers = {
        variant: {
            label: _call(vault, principal, command, **kwargs)
            for label, (command, kwargs) in _AGGREGATES.items()
        }
        for variant, vault in vaults.items()
    }

    for label in _AGGREGATES:
        assert {k: answers["A"][label].get(k) for k in _RESTRICTED} == _RESTRICTED, label
        assert _text(answers["A"][label]) == _text(answers["B"][label]), label
        assert _text(answers["C"][label]) == _text(answers["B"][label]), label
    owner = {
        label: _call(vaults["A"], None, command, **kwargs)
        for label, (command, kwargs) in _AGGREGATES.items()
    }
    assert "findings" in owner["review-audit"]
    assert "page_count" in owner["infer-categories"]
    assert all(answer.get("available") is not False for answer in owner.values())


_QUEUE_COUNT_FIELDS = (*_QUEUE_FIELDS, "pages_scanned", "pages_truncated", "pages_unscanned", "coverage")


@pytest.mark.parametrize("audience", AUDIENCES)
def test_relation_queue_counts_read_as_if_the_withheld_page_were_absent(
    tmp_path: Path, audience: str
) -> None:
    base, withheld = _inbound_linker()
    vaults = _twins(tmp_path, base, withheld, audience)
    principal = _principal(audience)

    answers = {}
    for variant, vault in vaults.items():
        queue = _call(vault, principal, "review_memory", mode="relation-queue")
        answers[variant] = {field: queue.get(field) for field in _QUEUE_COUNT_FIELDS}

    assert answers["A"]["coverage"] == _RESTRICTED
    assert _text(answers["A"]) == _text(answers["B"])
    assert _text(answers["C"]) == _text(answers["B"])
    owner = _call(vaults["A"], None, "review_memory", mode="relation-queue")
    assert owner["coverage"]["eligible_pages"] > 0


@pytest.mark.parametrize("audience", AUDIENCES)
def test_inbound_link_counts_follow_the_listed_links(tmp_path: Path, audience: str) -> None:
    base, withheld = _inbound_linker()
    vaults = _twins(tmp_path, base, withheld, audience)
    principal = _principal(audience)

    answers = {
        variant: {
            target: _call(vault, principal, "connect_memory", operation="inbound-links", target=target)
            for target in (f"{NOTES}/lonely.md", f"{NOTES}/beta.md")
        }
        for variant, vault in vaults.items()
    }

    assert _text(answers["A"]) == _text(answers["B"])
    assert _text(answers["C"]) == _text(answers["B"])
    for answer in answers["A"].values():
        assert answer["count"] == len(answer["inbound"])
    owner = _call(
        vaults["A"], None, "connect_memory", operation="inbound-links", target=f"{NOTES}/lonely.md"
    )
    assert owner["count"] == 1


def _term_only_withheld() -> tuple[dict[str, str], dict[str, str]]:
    """Only a withheld page contains the query term; a visible page shares a second term."""
    base = {
        **_filler(),
        f"{NOTES}/visible.md": _page(
            "Visible Note", "Quarterly review mentions pricing once.", type="insight"
        ),
    }
    withheld = {
        f"{WITHHELD_DIR}/memo.md": _page(
            "Memo", "Confidential zephyrine pricing terms for the review.", type="insight"
        )
    }
    return base, withheld


_ASK_SURFACES: dict[str, dict[str, Any]] = {
    "compact": {"query": "zephyrine", "limit": 5},
    "explain": {"query": "zephyrine", "limit": 5, "explain": True},
    "full": {"query": "pricing review", "limit": 5, "detail": "full"},
    "explain-shared": {"query": "pricing review", "limit": 5, "explain": True},
}


@pytest.mark.parametrize("audience", AUDIENCES)
def test_restricted_recall_diagnostics_read_as_if_the_withheld_page_were_absent(
    tmp_path: Path, audience: str
) -> None:
    base, withheld = _term_only_withheld()
    vaults = _twins(tmp_path, base, withheld, audience)
    principal = _principal(audience)

    answers = {}
    for variant, vault in vaults.items():
        _call(vault, principal, "ask_memory", **_ASK_SURFACES["compact"])  # warm
        answers[variant] = {
            label: _call(vault, principal, "ask_memory", **kwargs)
            for label, kwargs in _ASK_SURFACES.items()
        }

    for label in _ASK_SURFACES:
        answer = answers["A"][label]
        assert "__error__" not in _text(answer), answer
        assert "retrieval_profile" not in _text(answer), answer
        assert "ranking_explanation" not in _text(answer), answer
        assert _text(answer) == _text(answers["B"][label]), label
        assert _text(answers["C"][label]) == _text(answers["B"][label]), label
    owner = _call(vaults["A"], None, "ask_memory", **_ASK_SURFACES["explain"])
    assert "retrieval_profile" in owner


# ---------------------------------------------------------------------------
# Visible links resolve for a restricted reader as the vault it sees would
# ---------------------------------------------------------------------------

_LINKS_TO = "See [[{t}]] for background on the rollout.\n\n## Relations\n\n- supports [[{t}]]\n"


def _stem_collision() -> tuple[dict[str, str], dict[str, str]]:
    """A withheld page shares the stem a visible link names."""
    base = {
        **_filler(),
        f"{NOTES}/alpha.md": _page("Alpha", _LINKS_TO.format(t="beta"), type="insight"),
        f"{NOTES}/beta.md": _page("Beta", "Beta rollout background.", type="insight"),
    }
    withheld = {f"{WITHHELD_DIR}/beta.md": _page("Hidden Draft", "Withheld body text.", type="insight")}
    return base, withheld


def _title_collision() -> tuple[dict[str, str], dict[str, str]]:
    """A withheld page shares the title a visible link names."""
    base = {
        **_filler(),
        f"{NOTES}/alpha.md": _page("Alpha", _LINKS_TO.format(t="Beta Topic"), type="insight"),
        f"{NOTES}/2026-01-01-beta-topic.md": _page(
            "Beta Topic", "Beta topic rollout background.", type="insight", title="Beta Topic"
        ),
    }
    withheld = {
        f"{WITHHELD_DIR}/other-page.md": _page(
            "Beta Topic", "Withheld body text.", type="insight", title="Beta Topic"
        )
    }
    return base, withheld


_LINK_SCENARIOS = {
    "stem": (_stem_collision, f"{NOTES}/beta.md"),
    "title": (_title_collision, f"{NOTES}/2026-01-01-beta-topic.md"),
    "stem-beats-title": (_stem_beats_title, f"{NOTES}/g-page.md"),
}


def _link_surfaces(target: str) -> dict[str, tuple[str, dict[str, Any]]]:
    alpha = f"{NOTES}/alpha.md"
    return {
        "graph-context-alpha": ("connect_memory", {"operation": "graph-context", "path": alpha}),
        "graph-context-alpha-2": (
            "connect_memory",
            {"operation": "graph-context", "path": alpha, "depth": 2},
        ),
        "graph-context-target": ("connect_memory", {"operation": "graph-context", "path": target}),
        "context-alpha": ("connect_memory", {"operation": "context", "path": alpha}),
        "inbound-target": ("connect_memory", {"operation": "inbound-links", "target": target}),
        "suggest-alpha": ("connect_memory", {"operation": "suggest-relations", "path": alpha}),
        "relation-queue": ("review_memory", {"mode": "relation-queue"}),
        "read-alpha": ("read_memory", {"path": alpha, "links": True}),
        "read-target": ("read_memory", {"path": target, "links": True}),
    }


@pytest.mark.parametrize("scenario", sorted(_LINK_SCENARIOS))
@pytest.mark.parametrize("audience", AUDIENCES)
def test_restricted_link_resolution_reads_as_if_the_withheld_page_were_absent(
    tmp_path: Path, audience: str, scenario: str
) -> None:
    fixture, target = _LINK_SCENARIOS[scenario]
    base, withheld = fixture()
    vaults = _twins(tmp_path, base, withheld, audience)
    principal = _principal(audience)
    surfaces = _link_surfaces(target)

    answers = {
        variant: {
            label: _call(vault, principal, command, **kwargs)
            for label, (command, kwargs) in surfaces.items()
        }
        for variant, vault in vaults.items()
    }

    assert not _names_withheld(answers["A"])
    for label in surfaces:
        assert "__error__" not in answers["A"][label], answers["A"][label]
        assert _text(answers["A"][label]) == _text(answers["B"][label]), label
        assert _text(answers["C"][label]) == _text(answers["B"][label]), label
    edges = answers["A"]["graph-context-alpha"]["graph"]["edges"]
    assert any(edge["dst_key"] == f"file:{target}" for edge in edges), edges


@pytest.mark.parametrize("audience", AUDIENCES)
def test_a_restricted_reviewer_can_act_on_a_link_its_view_resolved(
    tmp_path: Path, audience: str
) -> None:
    base, withheld = _stem_collision()
    vaults = _twins(tmp_path, base, withheld, audience)
    principal = _principal(audience)

    answers = {}
    for variant, vault in vaults.items():
        queue = _call(vault, principal, "review_memory", mode="relation-queue")
        (item,) = [
            item
            for group in queue["groups"]
            for item in group["items"]
            if item["method"] == "wikilink"
        ]
        answer = _call(
            vault,
            principal,
            "triage_memory",
            ref=item["ref"],
            action="dismiss",
            source_path=item["source_path"],
        )
        # The decision's own timestamp differs between any two runs.
        stamped = re.sub(r'"updated_at": "[^"]*"', '"updated_at": ""', _text(answer))
        answers[variant] = json.loads(stamped)

    assert "__error__" not in answers["A"], answers["A"]
    assert _text(answers["A"]) == _text(answers["B"])
    assert _text(answers["C"]) == _text(answers["B"])


@pytest.mark.parametrize("audience", AUDIENCES)
def test_an_empty_provenance_list_stays_as_written(tmp_path: Path, audience: str) -> None:
    base = {
        **_filler(),
        f"{NOTES}/alpha.md": _page(
            "Alpha", "See [[zeta-plan]] for background.", type="insight", sources=[]
        ),
    }
    withheld = {f"{WITHHELD_DIR}/zeta-plan.md": _page("Hidden Draft", "Withheld body text.")}
    vaults = _twins(tmp_path, base, withheld, audience)

    answers = {
        variant: _call(
            vault, _principal(audience), "read_memory", path=f"{NOTES}/alpha.md", links=True
        )
        for variant, vault in vaults.items()
    }

    assert answers["A"]["frontmatter"]["sources"] == []
    assert _text(answers["A"]["frontmatter"]) == _text(answers["B"]["frontmatter"])


def test_the_owner_still_resolves_links_over_every_page(tmp_path: Path) -> None:
    base, withheld = _stem_collision()
    vault = _materialize(tmp_path / "vault", {**base, **withheld}, "external")

    owner = _call(
        vault, None, "connect_memory", operation="graph-context", path=f"{NOTES}/alpha.md"
    )

    # Over the whole vault the bare link is ambiguous, so no edge reaches beta.
    assert not any(
        edge["dst_key"] == f"file:{NOTES}/beta.md" for edge in owner["graph"]["edges"]
    )


# ---------------------------------------------------------------------------
# Activation resolves a turn over the anchors the caller may see
# ---------------------------------------------------------------------------


def _hub(h1: str, body: str, **frontmatter: Any) -> str:
    return _page(h1, body, type="hub", tags=["hub"], **frontmatter)


def _activation_base(hub: str) -> dict[str, str]:
    return {
        **_filler(),
        f"{NOTES}/orion-status.md": _page(
            "Orion Status",
            f"The Orion Program launch slipped two weeks. [[{NOTES}/orion-program]]",
            type="insight",
        ),
        f"{NOTES}/orion-program.md": hub,
    }


_PROGRAM_HUB = _hub(
    "Orion Program", f"Hub for the Orion Program. [[{NOTES}/orion-status]]", title="Orion Program"
)
_ACTIVATION_SCENARIOS: dict[str, tuple[str, dict[str, str]]] = {
    "same-title": (
        _PROGRAM_HUB,
        {f"{WITHHELD_DIR}/orion-private.md": _hub("Orion Program", "Withheld body text.", title="Orion Program")},
    ),
    "rarity": (
        _PROGRAM_HUB,
        {
            f"{WITHHELD_DIR}/orion-side-{index}.md": _hub(
                f"Orion Side {index}", "Withheld body text.", title=f"Orion Side {index}"
            )
            for index in range(3)
        },
    ),
    "derived-alias": (
        _hub(
            "Orion — Launch Plan",
            f"Hub for the Orion launch. [[{NOTES}/orion-status]]",
            title="Orion — Launch Plan",
        ),
        {f"{WITHHELD_DIR}/orion-private.md": _hub("Orion", "Withheld body text.", title="Orion")},
    ),
    "alias": (
        _hub(
            "Orion Program",
            f"Hub for the Orion Program. [[{NOTES}/orion-status]]",
            title="Orion Program",
            aliases=["OP-7"],
        ),
        {
            f"{WITHHELD_DIR}/orion-private.md": _hub(
                "Private Thing", "Withheld body text.", title="Private Thing", aliases=["OP-7"]
            )
        },
    ),
    "withheld-only": (
        _PROGRAM_HUB,
        {
            f"{WITHHELD_DIR}/nimbus-plan.md": _hub(
                "Nimbus Plan", "Withheld body text.", title="Nimbus Plan"
            )
        },
    ),
}
_TURNS = (
    "What is the status of the Orion Program?",
    "orion",
    "Tell me about OP-7",
    "What slipped in the Orion launch?",
    "What is in the Nimbus Plan?",
)


def _activated(vault: Path, principal: RequestPrincipal | None) -> dict[str, Any]:
    from exomem import lexstore, working_set_index, working_set_runtime

    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).reset()
    working_set_index.WorkingSetIndex(vault).rebuild()
    answers = {}
    for turn in _TURNS:
        packet = _call(vault, principal, "activate_context", turn=turn)
        answers[turn] = {
            key: value
            for key, value in packet.items()
            if key not in {"timings", "continuity", "generation"}
        }
        answers[turn]["generation"] = sorted((packet.get("generation") or {}).keys())
    return answers


@pytest.mark.parametrize("scenario", sorted(_ACTIVATION_SCENARIOS))
@pytest.mark.parametrize("audience", AUDIENCES)
def test_restricted_activation_reads_as_if_the_withheld_anchor_were_absent(
    tmp_path: Path, audience: str, scenario: str
) -> None:
    hub, withheld = _ACTIVATION_SCENARIOS[scenario]
    vaults = _twins(tmp_path, _activation_base(hub), withheld, audience)
    principal = _principal(audience)

    answers = {variant: _activated(vault, principal) for variant, vault in vaults.items()}

    assert not _names_withheld(answers["A"])
    for turn in _TURNS:
        packet = answers["A"][turn]
        assert "freshness_key" not in packet["generation"]
        assert (packet.get("abstention") or {}).get("reason") != "withheld", packet
        assert not [m for m in packet.get("missing") or () if m.get("reason") == "withheld"]
        assert _text(packet) == _text(answers["B"][turn]), turn
        assert _text(answers["C"][turn]) == _text(answers["B"][turn]), turn


def test_the_owner_still_activates_a_page_withheld_from_others(tmp_path: Path) -> None:
    hub, withheld = _ACTIVATION_SCENARIOS["withheld-only"]
    vault = _materialize(tmp_path / "vault", {**_activation_base(hub), **withheld}, "external")

    answers = _activated(vault, None)

    packet = answers["What is in the Nimbus Plan?"]
    assert [anchor["path"] for anchor in packet["anchors"]] == [f"{WITHHELD_DIR}/nimbus-plan.md"]
    assert "freshness_key" in packet["generation"]
