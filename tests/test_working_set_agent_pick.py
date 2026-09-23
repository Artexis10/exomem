"""U7 — `anchor` accepts any page a packet listed, not only an index anchor.

Design D3's retrieval carry already proved that an ordinary compiled page
(a research note, never an index anchor) can carry a packet: `_carried_packet`
and the units lane over it. This closes the other half — the agent naming
that SAME page itself with `anchor=` used to raise `INVALID_ANCHOR`, because
`override_candidates` only ever looked in the activation index's own rows.
It now falls back to the retrieval carry's own eligibility test
(`working_set._eligible_agent_page`) and reuses `_carried_packet` unchanged,
at the soundness rule's own "agent decides alone" outcome
(`status="resolved"`, `evidence=("agent_choice",)`) rather than invented
vocabulary.

The one refusal an unknown ref and a withheld one share now also covers raw
material, a navigation page, and a retired page named this way: none of
those is a second, distinguishable answer, because that would turn the
argument into an existence oracle.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from test_governance_egress import _external, _reset_caches, write_rule, write_scope
from test_working_set_carry import (
    CARRY_PAGE,
    CARRY_SOURCE,
    _seed_carry_pages,
    _seed_live_cell,
    _write,
)
from test_working_set_index import _seed_planning, _seed_structure

from exomem import commands, lexstore, working_set, working_set_index, working_set_runtime
from exomem.governance.principal import request_scope

#: A turn with nothing in the fixture catalogue's vocabulary at all. The
#: override branch never reads the turn's words to decide WHICH page —
#: `anchor` alone does that — so any turn this deliberately empty proves the
#: page was reached on the agent's choice and nothing else.
NONSENSE_TURN = "zqxwvu plonktastic frobnitz quibblewhomp"

#: A real anchor of `carry_vault`'s own catalogue (`_seed_structure`), for
#: the regression case: an `anchor` naming an index row must still behave
#: exactly as it did before this change.
REAL_ANCHOR = "Knowledge Base/Products/Cargo Sled.md"


@pytest.fixture
def carry_vault(vault: Path) -> Path:
    """A warm vault whose compiled pages include non-anchor research notes.

    Local copy of `test_working_set_carry.carry_vault` rather than a
    cross-file fixture import: importing a fixture function and also using
    its name as a test parameter reads to static analysis as redefinition
    (ruff F811), and no other module in this suite imports one across files.
    """
    _seed_structure(vault)
    _seed_planning(vault)
    _seed_carry_pages(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


def _anchor(packet: dict) -> dict:
    (only,) = packet["anchors"]
    return only


def test_an_agent_picked_research_note_returns_its_units_not_invalid_anchor(
    carry_vault: Path,
) -> None:
    """CARRY_PAGE is not an anchor of this index (`_seed_carry_pages`'s own
    docstring: "None is an anchor") — the exact ref an `unresolved` or
    `retrieval_named` packet would have listed for the agent to pick."""
    packet = commands.op_activate_context(
        carry_vault, turn=NONSENSE_TURN, anchor=CARRY_PAGE
    )

    assert packet["abstained"] is False, packet.get("abstention")
    anchor = _anchor(packet)
    assert anchor["ref"] == CARRY_PAGE
    assert anchor["kind"] == "page"
    assert anchor["status"] == "resolved"
    assert anchor["evidence"] == ["agent_choice"]
    assert packet["generation"]["carried_by"] == "agent_choice"
    assert packet["units"], "the page's own units must be served"
    assert all(
        unit["provenance"]["path"] == CARRY_PAGE for unit in packet["units"]
    ), packet["units"]


def test_a_real_anchor_override_is_unaffected(carry_vault: Path) -> None:
    """The half that must not change: an `anchor` naming an index row never
    reaches the new fallback (`resolution.status` is already `resolved`), so
    it keeps the ordinary role lanes and generation shape it always had."""
    packet = commands.op_activate_context(
        carry_vault, turn=NONSENSE_TURN, anchor=REAL_ANCHOR
    )

    assert packet["abstained"] is False, packet.get("abstention")
    anchor = _anchor(packet)
    assert anchor["ref"] == REAL_ANCHOR
    assert anchor["kind"] != "page"
    assert anchor["status"] == "resolved"
    assert anchor["evidence"] == ["agent_choice"]
    assert "carried_by" not in packet["generation"]


def test_the_withheld_ref_and_an_unknown_ref_receive_the_identical_refusal(
    carry_vault: Path,
) -> None:
    """The agent-pick fallback must cross the SAME egress guard a resolved
    override always did: a page this audience may not see is refused with
    the one word an unknown ref gets, never a second, distinguishable
    answer."""
    write_scope(carry_vault, paths="Knowledge Base/Notes/Research/*", name="Research")
    write_rule(carry_vault, ceiling=0)
    _reset_caches()
    working_set_runtime.reset_caches_for_tests()

    with pytest.raises(ValueError) as unknown:
        with request_scope(_external()):
            commands.op_activate_context(
                carry_vault,
                turn=NONSENSE_TURN,
                anchor="Knowledge Base/Notes/Research/no-such-page.md",
            )

    with pytest.raises(ValueError) as withheld:
        with request_scope(_external()):
            commands.op_activate_context(
                carry_vault, turn=NONSENSE_TURN, anchor=CARRY_PAGE
            )

    assert str(unknown.value) == str(withheld.value)
    assert "quillon" not in str(withheld.value).lower()
    assert "no-such-page" not in str(unknown.value)


@pytest.mark.parametrize(
    "make_ref",
    [
        pytest.param(lambda vault: CARRY_SOURCE, id="raw_material"),
        pytest.param(lambda vault: "Knowledge Base/index.md", id="navigation"),
    ],
)
def test_raw_material_and_navigation_refs_are_refused_like_unknown(
    carry_vault: Path, make_ref
) -> None:
    ref = make_ref(carry_vault)

    with pytest.raises(ValueError) as ineligible:
        commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=ref)

    with pytest.raises(ValueError) as unknown:
        commands.op_activate_context(
            carry_vault, turn=NONSENSE_TURN, anchor="Knowledge Base/Nowhere/absent.md"
        )

    assert str(ineligible.value) == str(unknown.value)


def test_a_retired_page_is_refused_like_unknown(carry_vault: Path) -> None:
    retired = "Knowledge Base/Notes/Research/quillon-vantry-window-old.md"
    _write(
        carry_vault / retired,
        """---
type: research-note
status: archived
updated: 2026-08-01
---

# Quillon vantry window (old)

## Summary

- [decision] The quillon vantry window used to be six minutes. ^q-old
""",
    )
    working_set_runtime.reset_caches_for_tests()

    with pytest.raises(ValueError) as retired_error:
        commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=retired)

    with pytest.raises(ValueError) as unknown:
        commands.op_activate_context(
            carry_vault, turn=NONSENSE_TURN, anchor="Knowledge Base/Nowhere/absent.md"
        )

    assert str(retired_error.value) == str(unknown.value)


def test_recent_context_still_leads_and_the_packet_still_fits_its_budget(
    carry_vault: Path,
) -> None:
    _seed_live_cell(carry_vault)

    packet = working_set.compile_packet(
        carry_vault, turn=NONSENSE_TURN, max_chars=4000, anchor=CARRY_PAGE
    )

    assert packet["abstained"] is False, packet.get("abstention")
    assert list(packet)[0] == "recent_context", list(packet)
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]
    assert packet["budget"]["used_chars"] <= 4000


# --------------------------------------------------------------------------- #
# The hook: the recent-context menu tells the agent which follow-up fits
# which entry, and the named-pages menu now offers the remedy that works.
# --------------------------------------------------------------------------- #


def test_a_captured_session_entry_is_labelled_for_read_memory() -> None:
    """A `Sources/Sessions` capture is raw material, so it stays a
    `read_memory` target even though the header's default follow-up for
    every other `recent_context` line is now `activate_context(anchor=...)`.
    """
    from exomem._hooks import exomem_retrieve_nudge as hook

    packet = {
        "recent_context": [
            {
                "ref": "Knowledge Base/Sources/Sessions/2026-09-20.md",
                "path": "Knowledge Base/Sources/Sessions/2026-09-20.md",
                "title": "2026-09-20",
                "kind": "page",
                "why": "captured",
                "as_of": "2026-09-20",
            }
        ],
        "anchors": [],
        "roles": [],
        "units": [],
        "pointers": [],
        "current_state": [],
        "ambiguity": [],
        "missing": [],
        "budget": {"limit_chars": 4000, "used_chars": 0},
        "generation": {},
        "abstained": False,
    }

    block = hook._format_working_set_block(packet, 4000)

    assert "- session:" in block, block
    assert "- recent:" not in block, block
    assert "read_memory" in hook._WORKING_SET_HEADER
    assert "activate_context" in hook._WORKING_SET_HEADER


def test_the_named_pages_menu_now_offers_the_anchor_remedy() -> None:
    """`retrieval_named` pages are exactly what `_eligible_agent_page` now
    admits, so the hook's own closing line for that menu must offer the
    remedy that actually works — `anchor=`, not `read_memory`, which is
    what a page not eligible this way still needs."""
    from exomem._hooks import exomem_retrieve_nudge as hook

    packet = {
        "recent_context": [],
        "anchors": [
            {
                "ref": "Knowledge Base/Notes/Research/a.md",
                "path": "Knowledge Base/Notes/Research/a.md",
                "title": "A",
                "kind": "page",
                "lifecycle": "active",
                "status": "retrieval_named",
                "evidence": ["retrieval"],
            },
            {
                "ref": "Knowledge Base/Notes/Research/b.md",
                "path": "Knowledge Base/Notes/Research/b.md",
                "title": "B",
                "kind": "page",
                "lifecycle": "active",
                "status": "retrieval_named",
                "evidence": ["retrieval"],
            },
        ],
        "roles": [],
        "units": [],
        "pointers": [],
        "current_state": [],
        "ambiguity": [],
        "missing": [],
        "budget": {"limit_chars": 4000, "used_chars": 0},
        "generation": {},
        "abstained": True,
        "abstention": {"reason": "unresolved"},
    }

    block = hook._format_working_set_block(packet, 4000)
    closing = block.splitlines()[-1]

    assert "anchor" in closing, closing
    assert "activate_context" in closing, closing
    assert "read_memory" not in closing, closing


def test_the_deployed_hook_mirror_stays_byte_identical() -> None:
    root = Path(__file__).resolve().parents[1]
    packaged = root / "src" / "exomem" / "_hooks" / "exomem_retrieve_nudge.py"
    deployed = root / "plugins" / "claude-code" / "hooks" / "exomem_retrieve_nudge.py"

    assert deployed.read_bytes() == packaged.read_bytes()


# --------------------------------------------------------------------------- #
# Follow-up round (batch review REQUEST_CHANGES, findings 5-10): a
# non-canonical spelling must not reach the expensive path or the file it
# names, a withheld page must refuse before the compile, the hook must route
# a unit/state/session ref to `read_memory`, and a resolved packet must still
# show its answer at a small hook ceiling.
# --------------------------------------------------------------------------- #


def _dot_dot_into_same_dir(ref: str) -> str:
    """`ref`, with its own last directory re-entered through a `..` —
    `posixpath.normpath` cancels it back to `ref` exactly, so this is a
    genuinely DIFFERENT spelling of the SAME canonical page, not a
    traversal to anywhere else."""
    directory, name = ref.rsplit("/", 1)
    parent_name = directory.rsplit("/", 1)[-1]
    return f"{directory}/../{parent_name}/{name}"


NON_CANONICAL_SPELLINGS = [
    pytest.param(lambda ref: "./" + ref, id="dot_prefix"),
    pytest.param(lambda ref: ref.replace("/", "//", 1), id="doubled_slash"),
    pytest.param(lambda ref: ref.replace("/", "\\"), id="backslash"),
    pytest.param(_dot_dot_into_same_dir, id="dot_dot_into_same_dir"),
]


@pytest.mark.parametrize("spell", NON_CANONICAL_SPELLINGS)
def test_a_non_canonical_spelling_of_an_eligible_page_is_refused_like_unknown(
    carry_vault: Path, spell
) -> None:
    """P1 (finding 5): every non-canonical spelling of CARRY_PAGE — a leading
    `./`, a doubled slash, a backslash, a `..` that lands back on the same
    page — refuses with the identical error an unknown ref gets, never the
    distinguishable, several-times-slower answer a spelling that reached
    `_carried_packet` used to give."""
    ref = spell(CARRY_PAGE)

    with pytest.raises(ValueError) as spelled:
        commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=ref)

    with pytest.raises(ValueError) as unknown:
        commands.op_activate_context(
            carry_vault, turn=NONSENSE_TURN, anchor="Knowledge Base/Nowhere/absent.md"
        )

    assert str(spelled.value) == str(unknown.value)


def test_an_absolute_path_is_refused_like_unknown(carry_vault: Path) -> None:
    ref = str(carry_vault / CARRY_PAGE)

    with pytest.raises(ValueError) as absolute:
        commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=ref)

    with pytest.raises(ValueError) as unknown:
        commands.op_activate_context(
            carry_vault, turn=NONSENSE_TURN, anchor="Knowledge Base/Nowhere/absent.md"
        )

    assert str(absolute.value) == str(unknown.value)


@pytest.mark.parametrize("spell", NON_CANONICAL_SPELLINGS)
def test_a_non_canonical_spelling_never_reaches_the_carried_packet_builder(
    carry_vault: Path, spell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stronger claim behind the refusal: it is not merely refused, it
    never reaches the expensive builder at all — never mind what an
    incidental match inside the units lane would or would not have caught.

    `CARRY_PAGE` itself is the control: eligible and canonical, it MUST
    still reach the builder and be served, or the other three assertions
    would prove nothing about a builder this vault never calls at all.
    """
    calls: list[object] = []
    real = working_set._carried_packet

    def spy(*args, **kwargs):
        calls.append(kwargs.get("page"))
        return real(*args, **kwargs)

    monkeypatch.setattr(working_set, "_carried_packet", spy)

    packet = commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=CARRY_PAGE)
    assert packet["abstained"] is False, packet.get("abstention")
    assert calls, "the control ref must reach the builder, or this proves nothing"
    calls.clear()

    for ref in (CARRY_SOURCE, spell(CARRY_PAGE), spell(CARRY_SOURCE)):
        with pytest.raises(ValueError):
            commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=ref)

    assert calls == [], calls


def test_an_unknown_or_non_canonical_ref_logs_nothing_at_warning(
    carry_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """P1's last bullet: the caller-supplied ref must not reach a WARNING
    log line. The structural fix (a catalogue lookup before any file read)
    already keeps every ref this test tries from ever reaching the read
    that used to log it."""
    refs = [
        "Knowledge Base/Nowhere/absent.md",
        "./" + CARRY_SOURCE,
        CARRY_SOURCE.replace("/", "//", 1),
        str(carry_vault / CARRY_PAGE),
    ]
    for ref in refs:
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ValueError):
                commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=ref)
        leaked = [
            record
            for record in caplog.records
            if record.levelno >= logging.WARNING and ref in record.getMessage()
        ]
        assert leaked == [], (ref, [r.getMessage() for r in leaked])


def test_the_canonicalizer_rejects_every_non_canonical_form_and_accepts_the_real_one(
    carry_vault: Path,
) -> None:
    """A direct, fast unit test of the gate itself, independent of the
    slower end-to-end refusal-equality tests above."""
    assert working_set._canonical_agent_page_ref(carry_vault, CARRY_PAGE) == CARRY_PAGE
    for bad in (
        "",
        "./" + CARRY_PAGE,
        CARRY_PAGE.replace("/", "//", 1),
        CARRY_PAGE.replace("/", "\\"),
        str(carry_vault / CARRY_PAGE),
        "Knowledge Base/Notes/Journal/../Research/quillon-vantry-window.md",
        "Knowledge Base/Nowhere/absent.md",
    ):
        assert working_set._canonical_agent_page_ref(carry_vault, bad) is None, bad


def test_a_withheld_agent_picked_page_is_refused_before_the_compile(
    carry_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 (finding 7): the release-plane decision for a picked page happens
    BEFORE the expensive compile, not after it — the compile buys nothing a
    withheld page can use, so it must never run for one."""
    write_scope(carry_vault, paths="Knowledge Base/Notes/Research/*", name="Research")
    write_rule(carry_vault, ceiling=0)
    _reset_caches()
    working_set_runtime.reset_caches_for_tests()

    calls: list[object] = []
    real = working_set._carried_packet

    def spy(*args, **kwargs):
        calls.append(kwargs.get("page"))
        return real(*args, **kwargs)

    monkeypatch.setattr(working_set, "_carried_packet", spy)

    with pytest.raises(ValueError):
        with request_scope(_external()):
            commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=CARRY_PAGE)

    assert calls == [], "a withheld page must never reach the compile"


def test_an_index_anchor_override_still_works_when_the_early_check_applies(
    carry_vault: Path,
) -> None:
    """The early release-plane check only ever short-circuits to the SAME
    refusal the ordinary path would reach; it must never prevent a VISIBLE
    ref, index anchor or agent-picked page, from being served."""
    packet = commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=REAL_ANCHOR)

    assert packet["abstained"] is False, packet.get("abstention")
    assert packet["anchors"][0]["ref"] == REAL_ANCHOR


def test_the_header_routes_by_kind() -> None:
    """P3 (finding 6): `unit`, `pointer`, `state` and `session` lines are not
    eligible `anchor` refs (a unit/state ref is not a page path at all, and a
    session capture is raw material) and must be routed to `read_memory`;
    every other kind keeps the `anchor=` default."""
    from exomem._hooks import exomem_retrieve_nudge as hook

    for kind in ("unit", "pointer", "state", "session"):
        assert f"`{kind}`" in hook._WORKING_SET_HEADER, hook._WORKING_SET_HEADER
    assert "read_memory" in hook._WORKING_SET_HEADER
    assert "activate_context(anchor=...)" in hook._WORKING_SET_HEADER


def test_a_resolved_packet_still_shows_its_answer_at_a_small_hook_ceiling() -> None:
    """P4 (finding 10): recent context must not consume the whole render at
    a small hook ceiling — capped to at most half of it, mirroring the
    packet's own `_budgeted_recent` reservation, so current state/units/
    pointers — the actual answer — still have room."""
    from exomem._hooks import exomem_retrieve_nudge as hook

    packet = {
        "recent_context": [
            {
                "ref": f"Knowledge Base/Notes/Journal/note-{i:02d}.md",
                "path": f"Knowledge Base/Notes/Journal/note-{i:02d}.md",
                "title": f"Ordinary note {i:02d}",
                "kind": "page",
                "why": "edited",
                "as_of": "2026-09-22",
                "statement": "status: an ordinary sentence about ordinary work, "
                "long enough to cost real characters on its own.",
            }
            for i in range(8)
        ],
        "anchors": [
            {
                "ref": "Knowledge Base/Products/Cargo Sled.md",
                "path": "Knowledge Base/Products/Cargo Sled.md",
                "title": "Cargo Sled",
                "kind": "resource",
                "lifecycle": "active",
                "status": "resolved",
                "evidence": ["exact_alias"],
            }
        ],
        "roles": [{"id": "resources", "source": "anchor_default", "lane": "units"}],
        "units": [
            {
                "ref": "exomem://vault/Knowledge%20Base/Products/Cargo%20Sled.md#unit-abc",
                "role": "resources",
                "text": "The cargo sled is rated for four hundred kilograms.",
                "lifecycle": "active",
                "updated": "2026-09-01",
                "provenance": {"path": "Knowledge Base/Products/Cargo Sled.md"},
            }
        ],
        "pointers": [],
        "current_state": [
            {
                "anchor": "Knowledge Base/Products/Cargo Sled.md",
                "statement": "state: in storage abroad",
            }
        ],
        "ambiguity": [],
        "missing": [],
        "budget": {"limit_chars": 4000, "used_chars": 0},
        "generation": {},
        "abstained": False,
    }

    block = hook._format_working_set_block(packet, 900)
    lines = block.splitlines()

    assert any(line.startswith("- recent:") for line in lines), block
    assert any(
        line.startswith("- unit:") or line.startswith("- state:") for line in lines
    ), block
    recent_cost = sum(len(line) + 1 for line in lines if line.startswith("- recent:"))
    assert recent_cost <= 900 // 2 + len(hook._WORKING_SET_HEADER), block


# --------------------------------------------------------------------------- #
# R-Q MINOR 7: every refused ref leaves at the same point, before the compile.
# --------------------------------------------------------------------------- #

_RETIRED_PICK = "Knowledge Base/Notes/Research/quillon-vantry-window-old.md"


@pytest.mark.parametrize(
    "ref",
    [
        pytest.param("Knowledge Base/Nowhere/absent.md", id="unknown"),
        pytest.param(CARRY_SOURCE, id="raw_material"),
        pytest.param("Knowledge Base/index.md", id="navigation"),
        pytest.param(_RETIRED_PICK, id="retired"),
        pytest.param(CARRY_PAGE, id="withheld"),
    ],
)
def test_every_refused_ref_leaves_before_the_compile(
    carry_vault: Path, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    """The withheld ref was refused before the compile and every other class
    after it, so a withheld ref answered about 3 ms FASTER than an unknown one
    (medians 17.9 and 21.2 ms): the refusal was one, the exit was not."""
    _write(
        carry_vault / _RETIRED_PICK,
        "---\ntype: research-note\nstatus: archived\n---\n\n# Old window\n\n"
        "- [decision] Six minutes. ^q-old\n",
    )
    if ref == CARRY_PAGE:
        write_scope(carry_vault, paths="Knowledge Base/Notes/Research/*", name="Research")
        write_rule(carry_vault, ceiling=0)
        _reset_caches()
    working_set_runtime.reset_caches_for_tests()
    compiled: list[object] = []
    real = commands.working_set_runtime_module.serve

    def spy(*args, **kwargs):
        compiled.append(kwargs.get("anchor"))
        return real(*args, **kwargs)

    monkeypatch.setattr(commands.working_set_runtime_module, "serve", spy)

    with pytest.raises(ValueError) as refused:
        with request_scope(_external()):
            commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=ref)

    assert str(refused.value) == commands.ACTIVATE_ANCHOR_REFUSAL
    assert compiled == [], "a refused ref must never reach the compile"

