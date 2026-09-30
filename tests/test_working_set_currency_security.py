"""Audience-view regressions from the temporal-currency security review.

External packets must be identical with a withheld page present or absent.
Owner controls prove filtering does not disable the underlying capability.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

import pytest

from exomem import context_roles, lexstore, working_set, working_set_currency, working_set_state
from exomem._hooks import exomem_retrieve_nudge as hook
from exomem.governance import egress
from exomem.governance.principal import request_scope
from test_governance_egress import _external
from test_working_set_temporal_currency import _governed_entity_vault

KB = "Knowledge Base"
ENTITY = f"{KB}/Entities/People/Ilse Vandermeer.md"
STANDING = f"{KB}/Notes/Patterns/harbor-study-operating-method.md"


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    # Independent edits, not a fixture-creation burst. Equal paths in the twins
    # have equal contact times, separated beyond the five-second burst window.
    stamp = date.today().toordinal() * 86400 - 719163 * 86400
    stamp -= 60 * (1 + int(hashlib.sha256(rel.encode()).hexdigest()[:8], 16) % 1000)
    os.utime(p, (stamp, stamp))


def _govern(vault: Path) -> None:
    from test_governance_egress import write_rule, write_scope
    from test_working_set_egress import _prepare_end_to_end_vault, _reset_governance_state

    _prepare_end_to_end_vault(vault)
    write_scope(vault)  # Notes/Patterns/**
    write_rule(vault, ceiling=0, audience="external")
    _reset_governance_state(vault)


def _ext(vault: Path, turn: str, **kw):
    from test_governance_egress import _external
    from test_working_set_egress import _reset_governance_state

    from exomem import commands
    from exomem.governance.principal import request_scope

    _reset_governance_state(vault)
    with request_scope(_external()):
        return commands.op_activate_context(vault, turn=turn, **kw)


def _owner(vault: Path, turn: str, **kw):
    from test_working_set_egress import _reset_governance_state

    from exomem import commands
    from exomem.governance.principal import RequestPrincipal, request_scope

    _reset_governance_state(vault)
    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        return commands.op_activate_context(vault, turn=turn, **kw)


def _shape(packet: dict, vault: Path) -> dict:
    """The disclosure-relevant surface, vault-root independent."""
    keep = {
        k: packet.get(k)
        for k in (
            "units",
            "pointers",
            "current_state",
            "missing",
            "anchors",
            "budget",
            "abstained",
            "abstention",
            "recent_context",
            "ambiguity",
        )
    }
    return json.loads(json.dumps(keep, default=str).replace(str(vault), "<V>"))


def _linked_canonical_vault(root: Path, *, with_secret: bool) -> Path:
    vault = root
    _write(
        vault,
        f"{KB}/Entities/Organizations/Harbourline Trust.md",
        (
            "---\ntype: entity\ntitle: Harbourline Trust\nstatus: active\nupdated: 2026-09-01\n"
            'current_state_page: "[[harbourline-operating-state]]"\n---\n\n# Harbourline Trust\n\n'
            "A shipping trust.\n\n## Related\n\nOperating state lives in [[harbourline-operating-state]]; "
            "the old plan is [[harbourline-kickoff]].\n"
        ),
    )
    if with_secret:
        _write(
            vault,
            f"{KB}/Notes/Patterns/harbourline-operating-state.md",
            (
                "---\ntype: note\nstatus: active\nupdated: 2026-09-20\n---\n\n"
                "# Harbourline operating state\n\n## Summary\n\n"
                "- [config] SECRETFACT the harbourline retainer is ninety thousand pounds and the "
                "renewal clause lets either party exit on thirty days notice from the quarter end ^retainer\n"
            ),
        )
    _write(
        vault,
        f"{KB}/Notes/harbourline-kickoff.md",
        (
            "---\ntype: note\nstatus: active\nupdated: 2026-02-01\n---\n\n"
            "# Harbourline kickoff\n\n## Summary\n\n"
            "- [fact] Harbourline scope was still under negotiation ^scope\n"
        ),
    )
    _govern(vault)
    return vault


def _profile_vault(root: Path, *, with_secret: bool) -> Path:
    vault = root
    _write(
        vault,
        f"{KB}/Products/Cargo Sled.md",
        (
            "---\ntype: note\nstatus: active\nupdated: 2026-09-02\n---\n\n# Cargo Sled\n\n"
            "## Summary\n\nA towed cargo sled rated for 400 kg.\n\n## Related\n\nSee [[sled-buyout-terms]].\n\n"
            "## Constraints\n\nNever exceed 400 kg.\n"
        ),
    )
    if with_secret:
        _write(
            vault,
            f"{KB}/Notes/Patterns/sled-buyout-terms.md",
            (
                "---\ntype: note\ntitle: SECRETTITLE Sled buyout with Northwind\nstatus: active\n"
                "updated: 2026-09-25\n---\n\n# SECRETTITLE Sled buyout with Northwind\n\n## Summary\n\n"
                "- [fact] SECRETBODY the buyout is priced at forty thousand ^price\n"
            ),
        )
    _govern(vault)
    return vault


def _study_vault(root: Path, *, with_standing: bool, decisions: int = 3) -> Path:
    vault = root
    _write(
        vault,
        f"{KB}/_Schema/project-keys.yaml",
        "projects:\n  harbor-study:\n    folder: Harbor Study\n    category: research\n",
    )
    _write(
        vault,
        ENTITY,
        (
            "---\ntype: entity\nentity_type: person\nstatus: active\nproject: harbor-study\n---\n\n"
            "# Ilse Vandermeer\n\n## Summary\n\nA hydrographer who reviews the harbour gauges.\n"
        ),
    )
    for i in range(decisions):
        _write(
            vault,
            f"{KB}/Notes/Decisions/gauge-decision-{i}.md",
            (
                f"---\ntype: note\nstatus: active\nupdated: 2026-09-1{i}\n---\n\n# Gauge decision {i}\n\n"
                f"## Summary\n\n- [decision] Gauge set {i} is reviewed by Ilse Vandermeer first each week ^d{i}\n\n"
                f"Related: [[{ENTITY.removesuffix('.md')}]]\n"
            ),
        )
    if with_standing:
        _write(
            vault,
            STANDING,
            (
                "---\ntype: note\nstatus: active\nupdated: 2025-01-01\nproject: harbor-study\nstanding: true\n---\n\n"
                "# Harbor study operating method\n\n## Summary\n\n"
                "- [decision] SECRETMETHOD every gauge claim is argued from the raw log first ^method\n"
            ),
        )
    _govern(vault)
    return vault


def _relay_vault(root: Path, *, with_successor: bool) -> Path:
    vault = root
    _write(
        vault,
        f"{KB}/Products/Relay Kit.md",
        (
            "---\ntype: note\nstatus: active\nupdated: 2026-09-02\n---\n\n# Relay Kit\n\n"
            "## Summary\n\nThe relay kit. Plan notes: [[relay-trial]].\n"
        ),
    )
    _write(
        vault,
        f"{KB}/Notes/Decisions/relay-trial.md",
        (
            "---\ntype: note\ntitle: Relay trial\nstatus: active\nupdated: 2026-09-20\n---\n\n# Relay trial\n\n"
            "## Summary\n\n"
            "- [decision] Considered transport A for the relay kit ^cand-a\n"
            "- [decision] Kept the relay kit budget fixed ^budget\n\n"
            "Related: [[Relay Kit]] [[relay-successor]]\n"
        ),
    )
    if with_successor:
        _write(
            vault,
            f"{KB}/Notes/Patterns/relay-successor.md",
            (
                "---\ntype: note\ntitle: Relay successor\nstatus: active\nupdated: 2026-09-28\n"
                'supersedes: "[[relay-trial]]"\n---\n\n# Relay successor\n\n'
                "## Decision\n- id: pick-b\n- relations: supersedes: [[relay-trial#cand-a]]\n\n"
                "SECRETSUCC Transport B replaces transport A for the relay kit.\n\n"
                "Related: [[Relay Kit]] [[relay-trial]]\n"
            ),
        )
    _govern(vault)
    return vault


def _frontmatter_canonical_vault(root: Path, *, with_secret: bool, body_link: bool) -> Path:
    link = " Operating notes: [[harbourline-operating-state]]." if body_link else ""
    _write(
        root,
        f"{KB}/Entities/Organizations/Harbourline Trust.md",
        (
            "---\ntype: entity\ntitle: Harbourline Trust\nstatus: active\nupdated: 2026-09-01\n"
            'current_state_page: "[[harbourline-operating-state]]"\n'
            'related: ["[[harbourline-operating-state]]"]\n---\n\n# Harbourline Trust\n\n'
            f"A shipping trust.{link}\n"
        ),
    )
    if with_secret:
        _write(
            root,
            f"{KB}/Notes/Patterns/harbourline-operating-state.md",
            (
                "---\ntype: note\nstatus: active\nupdated: 2026-09-20\n---\n\n"
                "# Harbourline operating state\n\n## Summary\n\n"
                "- [config] SECRETFACT the harbourline retainer is ninety thousand pounds ^retainer\n"
            ),
        )
    _govern(root)
    return root


def _neighbourhood_vault(root: Path, *, with_secret: bool) -> Path:
    _write(
        root,
        f"{KB}/Products/Cargo Sled.md",
        (
            "---\ntype: note\nupdated: 2026-09-02\n---\n\n# Cargo Sled\n\n"
            "## Summary\n\nA towed cargo sled rated for 400 kg.\n\n"
            "## Constraints\n\nNever exceed 400 kg.\n\n## Related\n\nRelated: [[sled-maintenance]] [[sled-buyout-terms]]\n"
        ),
    )
    _write(
        root,
        f"{KB}/Notes/sled-maintenance.md",
        (
            "---\ntype: note\ntitle: Sled maintenance\nstatus: active\nupdated: 2026-08-01\n---\n\n"
            "# Sled maintenance\n\nGrease the runners.\n"
        ),
    )
    if with_secret:
        _write(
            root,
            f"{KB}/Notes/Patterns/sled-buyout-terms.md",
            (
                "---\ntype: note\ntitle: SECRETTITLE Sled buyout with Northwind\nstatus: active\n"
                "updated: 2026-09-25\n---\n\n# SECRETTITLE Sled buyout with Northwind\n\nNothing here.\n"
            ),
        )
    _govern(root)
    return root


def _plain_vault(root: Path, *, with_secret: bool) -> Path:
    _write(
        root,
        f"{KB}/Products/Cargo Sled.md",
        (
            "---\ntype: note\nupdated: 2026-09-02\n---\n\n# Cargo Sled\n\n"
            "## Constraints\n\nNever exceed 400 kg.\n"
        ),
    )
    if with_secret:
        _write(
            root,
            f"{KB}/Notes/Patterns/unrelated-secret.md",
            (
                "---\ntype: note\ntitle: Unrelated secret\nstatus: active\nupdated: 2026-09-25\n---\n\n"
                "# Unrelated secret\n\nNothing.\n"
            ),
        )
    _govern(root)
    return root


def _entity(root: Path, *, declared: str, links: list[str]) -> None:
    related = "".join(f"- [[{l}]]\n" for l in links)
    _write(
        root,
        f"{KB}/Entities/Organizations/Harbourline Trust.md",
        (
            "---\ntype: entity\ntitle: Harbourline Trust\nstatus: active\nupdated: 2026-09-01\n"
            f'current_state_page: "{declared}"\n---\n\n# Harbourline Trust\n\n'
            "## Summary\n\nA shipping trust on the northern coast.\n\n"
            f"## Related\n\n{related}"
        ),
    )


def _p1c_vault(root: Path, *, with_secret: bool) -> Path:
    _entity(
        root,
        declared="[[harbourline-operating-state]]",
        links=["harbourline-operating-state", "harbourline-kickoff"],
    )
    if with_secret:
        _write(
            root,
            f"{KB}/Notes/Patterns/harbourline-operating-state.md",
            (
                "---\ntype: note\nstatus: active\nupdated: 2026-09-20\n---\n\n"
                "# Harbourline operating state\n\n## Summary\n\n"
                "- [config] SECRETFACT the harbourline retainer is ninety thousand pounds and the renewal "
                "clause lets either party exit on thirty days notice from the end of the quarter ^retainer\n"
            ),
        )
    _write(
        root,
        f"{KB}/Notes/harbourline-kickoff.md",
        (
            "---\ntype: note\nstatus: active\nupdated: 2026-02-01\n---\n\n# Harbourline kickoff\n\n"
            "## Summary\n\n- [fact] Harbourline scope was still under negotiation with the port board ^scope\n"
            "- [decision] Harbourline chose the northern berth for the first season of the trial ^berth\n"
        ),
    )
    _govern(root)
    return root


def _p6_vault(root: Path, *, with_secret: bool) -> Path:
    links = ["Notes/Insights/ops-state"] + (["Notes/Patterns/ops-state"] if with_secret else [])
    _entity(root, declared="[[ops-state]]", links=links)
    _write(
        root,
        f"{KB}/Notes/Insights/ops-state.md",
        (
            "---\ntype: note\nstatus: active\nupdated: 2026-09-10\n---\n\n# Ops state\n\n## Summary\n\n"
            "- [fact] Harbourline runs two tugs this season ^tugs\n"
        ),
    )
    if with_secret:
        _write(
            root,
            f"{KB}/Notes/Patterns/ops-state.md",
            (
                "---\ntype: note\nstatus: active\nupdated: 2026-09-12\n---\n\n# Ops state\n\n## Summary\n\n"
                "- [fact] SECRETOPS a third tug is being negotiated ^tug3\n"
            ),
        )
    _govern(root)
    return root


def _relay(root: Path, *, with_successor: bool) -> tuple[Path, set[str]]:
    trial = f"{KB}/Notes/Decisions/relay-trial.md"
    succ = f"{KB}/Notes/Patterns/relay-successor.md"
    _write(
        root,
        trial,
        (
            "---\ntype: note\ntitle: Relay trial\nstatus: active\nupdated: 2026-09-20\n---\n\n# Relay trial\n\n"
            "- [decision] Considered transport A for the relay kit ^cand-a\n"
            "- [decision] Kept the relay kit budget fixed ^budget\n"
        ),
    )
    if with_successor:
        _write(
            root,
            succ,
            (
                "---\ntype: note\ntitle: Relay successor\nstatus: active\nupdated: 2026-09-28\n"
                'supersedes: "[[relay-trial]]"\n---\n\n# Relay successor\n\n'
                "## Decision\n- id: pick-b\n- relations: supersedes: [[relay-trial#cand-a]]\n\n"
                "SECRETSUCC Transport B replaces transport A for the relay kit.\n"
            ),
        )
    _govern(root)
    return root, {trial, succ} if with_successor else {trial}


def _p6b(root: Path, *, with_secret: bool) -> Path:
    # Entity links ONLY the visible page; the withheld same-stem page links back.
    _entity(root, declared="[[ops-state]]", links=["Notes/Insights/ops-state"])
    _write(
        root,
        f"{KB}/Notes/Insights/ops-state.md",
        (
            "---\ntype: note\nstatus: active\nupdated: 2026-09-10\n---\n\n# Ops state\n\n## Summary\n\n"
            "- [fact] Harbourline runs two tugs this season ^tugs\n"
        ),
    )
    if with_secret:
        _write(
            root,
            f"{KB}/Notes/Patterns/ops-state.md",
            (
                "---\ntype: note\nstatus: active\nupdated: 2026-09-12\n---\n\n# Ops state\n\n## Summary\n\n"
                "- [fact] SECRETOPS a third tug is being negotiated ^tug3\n\nAbout [[Harbourline Trust]].\n"
            ),
        )
    _govern(root)
    return root


def _p1d(root: Path, *, with_secret: bool) -> Path:
    facts = [f"harbourline-fact-{i}" for i in range(4)]
    _entity(
        root,
        declared="[[harbourline-operating-state]]",
        links=["harbourline-operating-state", *facts],
    )
    if with_secret:
        _write(
            root,
            f"{KB}/Notes/Patterns/harbourline-operating-state.md",
            (
                "---\ntype: note\nstatus: active\nupdated: 2026-09-20\n---\n\n# Harbourline operating state\n\n"
                "## Summary\n\n- [config] SECRETFACT " + "x" * 170 + " ^retainer\n"
            ),
        )
    for i, f in enumerate(facts):
        _write(
            root,
            f"{KB}/Notes/{f}.md",
            (
                f"---\ntype: note\nstatus: active\nupdated: 2026-08-0{i + 1}\n---\n\n# Harbourline fact {i}\n\n"
                f"## Summary\n\n- [decision] Harbourline decided item {i}: the port board approved the "
                f"northern berth schedule revision number {i} for the coming season ^f{i}\n"
            ),
        )
    _govern(root)
    return root


def _assert_twins(a, b, turn, **kwargs):
    pa, pb = _ext(a, turn, **kwargs), _ext(b, turn, **kwargs)
    assert _shape(pa, a) == _shape(pb, b)
    assert "SECRET" not in json.dumps(pa)
    return pa


@pytest.mark.parametrize("max_chars", [4000, 500, 700])
@pytest.mark.parametrize("fixture", [_linked_canonical_vault, _p1c_vault])
def test_withheld_canonical_page_has_no_budget_or_packet_effect(tmp_path, fixture, max_chars):
    """P1/P1c (D5): dropping current state must not spend the released budget."""
    a = fixture(tmp_path / "a", with_secret=True)
    b = fixture(tmp_path / "b", with_secret=False)
    turn = "what is the current state of Harbourline Trust"
    _assert_twins(a, b, turn, max_chars=max_chars)
    assert "SECRETFACT" in json.dumps(_owner(a, turn))


def test_frontmatter_only_canonical_page_has_no_budget_effect(tmp_path):
    """P1b (D5): neither twin's lede links the canonical page."""
    a = _frontmatter_canonical_vault(tmp_path / "a", with_secret=True, body_link=False)
    b = _frontmatter_canonical_vault(tmp_path / "b", with_secret=False, body_link=False)
    turn = "what is the current state of Harbourline Trust"
    _assert_twins(a, b, turn)
    _assert_owner_material(a, f"{KB}/Notes/Patterns/harbourline-operating-state.md", "SECRETFACT")


@pytest.mark.parametrize("max_chars", [500, 600, 700])
def test_withheld_canonical_statement_does_not_displace_visible_units(tmp_path, max_chars):
    """P1d (D5): preserve units, pointers and used_chars under budget pressure."""
    a = _p1d(tmp_path / "a", with_secret=True)
    b = _p1d(tmp_path / "b", with_secret=False)
    turn = "what did Harbourline Trust decide about the berth"
    _assert_twins(a, b, turn, max_chars=max_chars)
    assert "SECRETFACT" in json.dumps(_owner(a, turn))


@pytest.mark.parametrize("fixture", [_profile_vault, _neighbourhood_vault])
def test_withheld_neighbourhood_note_cannot_supply_title_or_date(tmp_path, fixture):
    """P2/P2b (D2): choose the newest note from the audience's neighbourhood."""
    a = fixture(tmp_path / "a", with_secret=True)
    b = fixture(tmp_path / "b", with_secret=False)
    turn = "what are the constraints on the cargo sled"
    packet = _assert_twins(a, b, turn)
    owner = _owner(a, turn)
    if fixture is _neighbourhood_vault:
        assert "SECRETTITLE" in json.dumps(owner["current_state"])
    else:
        assert "SECRETBODY" in json.dumps(owner)
    assert "2026-09-25" not in json.dumps(packet)


def test_withheld_standing_unit_does_not_take_a_role_slot(tmp_path):
    """P3 (D3): standing material must be filtered before role caps."""
    a = _study_vault(tmp_path / "a", with_standing=True)
    b = _study_vault(tmp_path / "b", with_standing=False)
    turn = "Ilse Vandermeer asked whether the harbour gauges look healthy this week."
    _assert_twins(a, b, turn)
    owner = _owner(a, turn)
    assert any(
        "SECRETMETHOD" in u["text"] and u["provenance"].get("standing") for u in owner["units"]
    )


def test_withheld_reverse_supersession_does_not_change_released_units(tmp_path):
    """P4: supersession authored on another page does not demote released units."""
    a = _relay_vault(tmp_path / "a", with_successor=True)
    b = _relay_vault(tmp_path / "b", with_successor=False)
    turn = "what did we decide about the relay kit transport"
    _assert_twins(a, b, turn)
    _assert_owner_material(a, f"{KB}/Notes/Patterns/relay-successor.md", "SECRETSUCC")


def test_withheld_reverse_supersession_does_not_change_lane_or_hook(tmp_path):
    """P4b: the filtered lane, budget and hook agree with an absent successor."""
    packets = {}
    for name, with_successor in (("a", True), ("b", False)):
        root, hood = _relay(tmp_path / name, with_successor=with_successor)
        lexstore.ensure_fresh(root)
        registry = context_roles.load_roles()
        with request_scope(_external()):
            items, missing = working_set.run_lanes(
                root,
                anchors=(),
                roles=({"id": "recent_change"},),
                registry=registry,
                neighbourhood=frozenset(hood),
                visible=working_set._reader_view(root, None),
            )
            packet = working_set.build_packet(
                items=items,
                anchors=(),
                roles=(),
                current_state=(),
                ambiguity=(),
                missing=missing,
                max_chars=4000,
                generation={},
                status="resolved",
            )
            release = egress.AnnotatedHits(
                hits=[], withheld_paths=frozenset(), active=True, blocked=False
            )
            packet = egress.guard_working_set(root, packet, release)
        packets[name] = (_shape(packet, root), hook._packet_lines(packet))
        if with_successor:
            owner_items, _ = working_set.run_lanes(
                root,
                anchors=(),
                roles=({"id": "recent_change"},),
                registry=registry,
                neighbourhood=frozenset(hood),
            )
            assert any("SECRETSUCC" in item.text for item in owner_items)
    assert packets["a"] == packets["b"]


def test_unlinked_withheld_note_has_no_recent_context_effect(tmp_path):
    """P5: adding an unrelated restricted page cannot change the visible packet."""
    a = _plain_vault(tmp_path / "a", with_secret=True)
    b = _plain_vault(tmp_path / "b", with_secret=False)
    turn = "what are the constraints on the cargo sled"
    _assert_twins(a, b, turn)
    assert "unrelated-secret.md" in json.dumps(_owner(a, turn)["recent_context"])


@pytest.mark.parametrize("fixture", [_p6_vault, _p6b])
def test_withheld_same_stem_page_does_not_make_current_state_ambiguous(tmp_path, fixture):
    """P6/P6b (D4): direct links and backlinks count only visible candidates."""
    a = fixture(tmp_path / "a", with_secret=True)
    b = fixture(tmp_path / "b", with_secret=False)
    turn = "what is the current state of Harbourline Trust"
    packet = _assert_twins(a, b, turn)
    assert _owner(a, turn)["current_state"] == []
    _assert_owner_material(a, f"{KB}/Notes/Patterns/ops-state.md", "SECRETOPS")
    assert "two tugs" in json.dumps(packet["current_state"])


CUE_TURNS = [
    "what is Harbourline Trust doing right now",
    "what is the Harbourline Trust retainer currently",
    "how much is the Harbourline Trust retainer",
    "what is the current state of Harbourline Trust",
]


@pytest.mark.parametrize("turn", CUE_TURNS)
def test_current_state_records_cues_release_only_the_audience_view(tmp_path, turn):
    """P5/P6 HIGH-1 (D1): cue twins with public ledes independent of private links."""
    a = _p1c_vault(tmp_path / "a", with_secret=True)
    b = _p1c_vault(tmp_path / "b", with_secret=False)
    _assert_twins(a, b, turn)
    assert "SECRETFACT" in json.dumps(_owner(a, turn))


def test_records_lane_preserves_the_current_state_path():
    """P6 (D1): defence in depth lets the guard decide a Records lane entry."""
    path = f"{KB}/Notes/Patterns/operating-state.md"
    entries = ({"anchor": "a", "path": path, "statement": "state"},)
    items = working_set._records_lane(
        context_roles.load_roles().roles["current_state"], current_state=entries
    )
    assert items[0].path == path


@pytest.mark.parametrize(
    "page_updated", ["not-a-date " * 40, "2026-02-30", "2026-09-20" + "x" * 440]
)
def test_page_updated_labels_are_validated_and_bounded(page_updated):
    """P7 (D6): hostile frontmatter cannot break the 200-character bound."""
    statement = "S" * working_set_state.STATEMENT_MAX_CHARS
    entry = {
        "anchor": "a",
        "source": "canonical_page",
        "path": "p.md",
        "as_of": "",
        "statement": statement,
        "page_updated": page_updated,
    }
    packet = working_set.build_packet(
        items=(),
        anchors=(),
        roles=(),
        current_state=(entry,),
        ambiguity=(),
        missing=(),
        max_chars=4000,
        generation={},
        status="resolved",
    )
    rendered = packet["current_state"][0]["statement"]
    assert len(rendered) <= working_set_state.STATEMENT_MAX_CHARS
    if page_updated.startswith("2026-09-20"):
        assert rendered.endswith(" (page updated 2026-09-20)")
    else:
        assert rendered == statement
    assert packet["budget"]["used_chars"] == len(rendered)


def test_own_time_caps_hostile_context_scan():
    """D7: bounded work when every earlier date is invalid or a deadline."""
    hostile = "2026-13-45 due 2026-09-20; " * 1000
    assert working_set_currency.own_time(hostile + "measured 2026-09-21") == ""
    assert working_set_currency.own_time("x" * 512 + "2026-09-21") == ""


def test_own_time_deadline_cue_search_has_a_fixed_window(monkeypatch):
    """D7: no growing prefix is scanned for each date in the bounded context."""
    cue = working_set_currency._DEADLINE_CUE
    lengths = []

    class CueProbe:
        def search(self, text):
            lengths.append(len(text))
            return cue.search(text)

    monkeypatch.setattr(working_set_currency, "_DEADLINE_CUE", CueProbe())
    assert working_set_currency.own_time("2026-13-45 due 2026-09-20; " * 1000) == ""
    assert lengths and max(lengths) <= 32


def _assert_owner_material(root, path, marker):
    from exomem.governance.principal import RequestPrincipal

    lexstore.ensure_fresh(root)
    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        registry = context_roles.load_roles()
        items, _ = working_set.run_lanes(
            root,
            anchors=(),
            roles=tuple({"id": role} for role in registry.roles),
            registry=registry,
            neighbourhood=frozenset({path}),
        )
    assert any(marker in item.text for item in items)


@pytest.mark.parametrize("turn", CUE_TURNS)
def test_original_high1_fixture_never_leaks_on_records_cues(tmp_path, turn):
    """The author's original lede has a withheld link: the guard drops it whole."""
    vault = _governed_entity_vault(tmp_path, declared=True)
    assert "SECRETFACT" not in json.dumps(_ext(vault, turn))
    assert "SECRETFACT" in json.dumps(_owner(vault, turn))
