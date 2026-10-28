"""Topic turns: the page a turn names in ordinary words, and the thread that carries it.

A turn can name an ordinary note by its title in everyday words: "the smooth
chain cleaning" names the note titled "Smooth chain cleaning". No anchor
resolves, and no two words of the turn are distinctive, so the carry rightly
serves nothing. The packet used to abstain listing hubs that recall alone had
surfaced, and never the note the turn named. It now lists that note for the
agent to pick, and stops listing what recall alone surfaced.

A keyless client (one that passes no `session`) that echoes the token keeps
its thread: the page its first packet served is what a later short follow-up
resumes.

Invented names throughout, on a synthetic bicycle-workshop vault.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from test_governance_egress import SCOPE_ID, _external, _gov_dir, _reset_caches, write_rule
from test_working_set_carry import seed_ordinary_notes

from exomem import commands, lexstore, working_set_heat, working_set_index, working_set_runtime
from exomem.governance.principal import request_scope

# This module tests the `anchors` packet, the rollback mode; `maps` is the shipped default.
pytestmark = pytest.mark.usefixtures("anchors_packet")

KB = "Knowledge Base"
WORKSHOP = f"{KB}/Notes/Research/Workshop"
CLEANING = f"{WORKSHOP}/smooth-chain-cleaning.md"
TRUING = f"{WORKSHOP}/wheel-truing-notes.md"
WITHHELD = f"{KB}/Notes/Restricted/ride-log-handbook.md"

#: Everyday prose that makes the turns' ordinary words ordinary in this vault.
EVERYDAY = (
    "A good week overall: the smooth plan held, the new routine works best in the morning, "
    "and I find a long walk after lunch helps. The weekend was quiet, the commute felt easy, "
    "and I usually go back and forth before I decide and check twice. Monday brought a short "
    "flight home and a great evening with friends; the rest was done later in the week."
)


def _write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _note(
    vault: Path,
    rel: str,
    title: str,
    units: list[tuple[str, str]],
    *,
    project: str | None = None,
    type_: str = "note",
    tags: tuple[str, ...] = (),
    updated: str = "2026-09-20",
) -> None:
    front = [f"type: {type_}", "status: active", f"updated: {updated}"]
    if project:
        front.append(f"project: {project}")
    if tags:
        front.append("tags: [" + ", ".join(tags) + "]")
    body = "\n".join(
        f"- [{category}] {text} ^u{index}" for index, (category, text) in enumerate(units)
    )
    _write(vault, rel, "---\n" + "\n".join(front) + f"\n---\n\n# {title}\n\n## Summary\n\n{body}\n")


def _seed_workshop_vault(vault: Path) -> None:
    """Everyday prose, two workshop notes that are not anchors, thirty ride
    logs, and two hubs and a resource whose text shares everyday words with
    the turns below but whose names share none."""
    seed_ordinary_notes(vault, 120)
    for index in range(60):
        _note(
            vault,
            f"{KB}/Notes/Journal/everyday-{index:03d}.md",
            f"Everyday note {index:03d}",
            [("note", EVERYDAY)],
            updated="2026-08-02",
        )
    _write(
        vault,
        f"{KB}/_Schema/project-keys.yaml",
        "projects:\n"
        "  cycling: {folder: Cycling, category: domain}\n"
        "  music: {folder: Music, category: domain}\n"
        "  garden: {folder: Garden, category: domain}\n",
    )
    _note(
        vault,
        CLEANING,
        "Smooth chain cleaning",
        [
            (
                "method",
                "Soak the chain in degreaser, scrub each link with a stiff brush and dry "
                "it before oiling.",
            ),
            ("preference", "One drop of oil per roller is enough."),
        ],
        project="cycling",
        type_="research-note",
        updated="2026-07-02",
    )
    _note(
        vault,
        TRUING,
        "Wheel truing notes",
        [
            ("method", "Check spoke tension on the truing stand, a quarter turn at a time."),
            ("method", "Let the wheel settle overnight and recheck the tension before riding."),
        ],
        project="cycling",
        type_="research-note",
        updated="2026-07-03",
    )
    for index in range(30):
        _note(
            vault,
            f"{WORKSHOP}/ride-log-{index:02d}.md",
            f"Ride log {index:02d}",
            [
                (
                    "observation",
                    "Chain cleaning after the wet ride again; the chain slipped twice "
                    "and the gears needed more oil.",
                ),
            ],
            project="cycling",
            updated=f"2026-09-{(index % 28) + 1:02d}",
        )
    _note(
        vault,
        f"{KB}/Notes/Hubs/choir-rehearsal-plan.md",
        "Choir rehearsal plan",
        [
            (
                "principle",
                "Warm-ups work best when the room is quiet; a smooth start sets the tone.",
            ),
        ],
        project="music",
        tags=("hub",),
    )
    _note(
        vault,
        f"{KB}/Notes/Hubs/allotment.md",
        "Allotment",
        [
            ("plan", "Find the best spot for the beans; cleaning the tools after use works well."),
        ],
        project="garden",
        tags=("hub",),
    )
    _note(
        vault,
        f"{KB}/Products/Laptop.md",
        "Laptop",
        [
            (
                "fact",
                "Runs smooth after the cleaning; the fan works fine and the battery is the "
                "best it has been.",
            ),
        ],
        tags=("resource",),
    )


def _refresh(vault: Path) -> None:
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    _reset_caches()
    working_set_runtime.reset_caches_for_tests()


@pytest.fixture
def workshop_vault(vault: Path) -> Path:
    _seed_workshop_vault(vault)
    _refresh(vault)
    working_set_heat.reset_for_tests()
    return vault


def _activate(vault: Path, turn: str, **kwargs: Any) -> dict[str, Any]:
    working_set_runtime.reset_caches_for_tests()
    return commands.op_activate_context(vault, turn=turn, **kwargs)


# --------------------------------------------------------------------------- #
# A page named by its title in ordinary words is listed, never served
# --------------------------------------------------------------------------- #


def test_a_topic_turn_in_ordinary_words_lists_the_page_its_title_names(
    workshop_vault: Path,
) -> None:
    """The turn says the note's title in everyday words. It must be listed
    for the agent to pick, with its project, while nothing is served and no
    hub that recall alone surfaced is listed beside it; picking it serves it.
    Fails if the title listing is removed (no note), if retrieval-only
    partials are listed again (the hubs come back), or if a shared title word
    without its neighbour names the page (the twins list it)."""
    turn = "The smooth chain cleaning works best with degreaser, I find."
    packet = _activate(workshop_vault, turn)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unresolved"}
    assert [
        (anchor["path"], anchor["status"], anchor["evidence"], anchor.get("projects"))
        for anchor in packet["anchors"]
    ] == [(CLEANING, "retrieval_named", ["lexical_overlap", "retrieval"], ["cycling"])]
    assert packet["units"] == [] and packet["pointers"] == [] and packet["current_state"] == []
    assert packet["budget"]["used_chars"] == 0
    assert CLEANING not in working_set_runtime.decode_continuity(packet["continuity"])["refs"]

    picked = _activate(workshop_vault, turn, anchor=CLEANING)

    assert picked["abstained"] is False
    assert {unit["provenance"]["path"] for unit in picked["units"]} == {CLEANING}

    one_word = _activate(
        workshop_vault, "We had a smooth flight home and a great weekend with friends."
    )
    apart = _activate(
        workshop_vault,
        "The cleaning was done by Monday, and later in the week I will oil the chain.",
    )

    assert one_word["abstained"] is True and one_word["anchors"] == []
    assert apart["abstained"] is True and apart["anchors"] == []


@pytest.mark.parametrize(
    ("title", "turn", "listed"),
    [
        ("On call", "The on call work again", True),
        ("By design", "The by design work again", True),
        ("On call", "The on work again. Call later.", False),
        ("On call", "The call was done by Monday, and later in the week I will be on.", False),
        ("蒸気 pressure", "The 蒸気 pressure works again", True),
        ("蒸気 圧力", "The 蒸気 圧力 works again", True),
        ("Rotation rotating", "The rotation rotating works again", False),
        ("蒸気 圧力", "The 蒸気圧力 works again", False),
        ("蒸気圧力 pressure", "The 蒸気 pressure works again", False),
    ],
)
def test_title_choices_keep_function_words_and_distinct_run_contacts(
    workshop_vault: Path, title: str, turn: str, listed: bool
) -> None:
    """A menu keeps two title units, but repetitions or fragments of one run
    cannot supply the second contact."""
    path = f"{WORKSHOP}/contact-note.md"
    _note(workshop_vault, path, title, [("method", "The plan works again.")], type_="research-note")
    _refresh(workshop_vault)

    packet = _activate(workshop_vault, turn)

    assert packet["abstention"] == {"reason": "unresolved"}
    assert any(item["path"] == path for item in packet["anchors"]) is listed
    assert packet["units"] == [] and packet["pointers"] == [] and packet["current_state"] == []
    assert path not in working_set_runtime.decode_continuity(packet["continuity"])["refs"]


def test_title_listing_preserves_the_budget_of_existing_recent_context(workshop_vault: Path) -> None:
    """Listing a choice adds no material charge to the prior thread's context."""
    first = _activate(
        workshop_vault, "For the back rim, should I check the spoke tension on the truing stand?"
    )
    packet = _activate(
        workshop_vault,
        "The smooth chain cleaning works best with degreaser, I find.",
        continuity=first["continuity"],
    )

    assert any(item["path"] == CLEANING for item in packet["anchors"])
    assert packet["units"] == [] and packet["pointers"] == [] and packet["current_state"] == []
    assert any(item["path"] == TRUING for item in packet["recent_context"])
    assert packet["budget"]["used_chars"] == sum(
        len(item["title"]) for item in packet["recent_context"]
    )
    assert CLEANING not in working_set_runtime.decode_continuity(packet["continuity"])["refs"]


# --------------------------------------------------------------------------- #
# A keyless conversation keeps what its first packet served
# --------------------------------------------------------------------------- #


def test_a_keyless_follow_up_resumes_the_page_its_first_packet_served(workshop_vault: Path) -> None:
    """A client that passes no `session` but echoes every token: the first
    packet carries the truing notes, the second turn abstains, and the short
    follow-up must still be carried on the truing notes. Fails when the first
    packet of a fresh thread is not recorded under that thread: the thread
    then holds nothing, and the follow-up abstains with no anchor."""
    first = _activate(
        workshop_vault, "For the back rim, should I check the spoke tension on the truing stand?"
    )
    assert [(anchor["path"], anchor["status"]) for anchor in first["anchors"]][:1] == [
        (TRUING, "retrieval_carried")
    ]
    second = _activate(
        workshop_vault, "How long should it settle after that?", continuity=first["continuity"]
    )
    assert second["abstained"] is True

    third = _activate(workshop_vault, "and the timing?", continuity=second["continuity"])

    assert third["generation"]["continuity_thread"] == "applied"
    assert third["generation"].get("carried_by") == "follow_up", (
        third.get("abstention"),
        third["anchors"],
    )
    assert [(anchor["path"], anchor["status"]) for anchor in third["anchors"]] == [
        (TRUING, "partial")
    ]


# --------------------------------------------------------------------------- #
# Governance: a withheld page named by its title answers as an absent one
# --------------------------------------------------------------------------- #


def _withhold(vault: Path, path: str) -> None:
    scope = _gov_dir(vault) / "scopes" / "patterns.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        f'governance_version: 1\nid: {SCOPE_ID}\nname: Withheld\npaths: ["{path}"]\n',
        encoding="utf-8",
    )
    write_rule(vault, ceiling=0)


def _guest_over_mcp(vault: Path, monkeypatch: pytest.MonkeyPatch, turn: str) -> dict[str, Any]:
    """One external caller's `activate_context` through the built MCP server,
    its terminal egress pass included, without the field that differs between
    any two calls (the token's random thread)."""
    from exomem import server as server_module

    _refresh(vault)
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    for name in (
        "EXOMEM_DISABLE_EMBEDDINGS",
        "EXOMEM_DISABLE_RELEVANCE_CHECK",
        "EXOMEM_DISABLE_FILE_WATCHER",
    ):
        monkeypatch.setenv(name, "1")
    mcp = server_module.build_server(require_auth=False)
    with request_scope(_external()):
        result = asyncio.run(
            mcp.call_tool("activate_context", {"turn": turn}, run_middleware=False)
        )
    payload = result.structured_content
    if isinstance(payload, dict):
        packet = payload.get("result", payload) if set(payload) == {"result"} else payload
    else:
        packet = json.loads(result.content[0].text)
    packet = json.loads(json.dumps(packet, default=str))
    packet.pop("continuity", None)
    return packet


def test_a_withheld_page_whose_title_the_turn_names_answers_as_an_absent_one(
    workshop_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thirty ride logs answer to "ride log"; a withheld handbook answers to
    it best of all. The guest must get exactly the packet of a vault without
    the handbook: the same three logs, in the same order. A listing ranked
    over pages the guest may not see, and capped before they are removed,
    shows two logs, which says a page it may not see outranked the third."""
    _write(
        workshop_vault,
        WITHHELD,
        "---\ntype: note\nstatus: active\nupdated: 2026-09-30\n---\n\n# Ride log handbook\n\n"
        "## Summary\n\n- [method] Keep the ride log daily: one ride log line per ride, and file "
        "the ride log weekly. ^h0\n",
    )
    _withhold(workshop_vault, WITHHELD)
    turn = "Can you make me a ride log template?"

    restricted = _guest_over_mcp(workshop_vault, monkeypatch, turn)
    (workshop_vault / WITHHELD).unlink()
    absent = _guest_over_mcp(workshop_vault, monkeypatch, turn)

    named = [
        anchor["path"] for anchor in absent["anchors"] if anchor["status"] == "retrieval_named"
    ]
    assert len(named) == 3 and all(path.startswith(f"{WORKSHOP}/ride-log-") for path in named)
    assert restricted == absent
