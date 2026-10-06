"""A carried page the turn names by its own title is resolved.

The retrieval carry reported every page it carried as `retrieval_carried`,
"no anchor was named", even when the turn said the page's title: "what's on
the kelvane intake checklist?" names the note "Kelvane intake checklist" as
plainly as a turn names an anchor. Now the carried page earns the anchor
rule's own name contact (two or more shared authored title terms) as
`lexical_overlap` beside its `retrieval`, and the existing soundness rule
resolves it. Admission is unchanged: a page only a body phrase reached, or
one title word, stays carried, and a phrase two pages answer to still asks.
Owner ruling of 2026-10-05.

Invented names throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_working_set_carry import seed_ordinary_notes

from exomem import commands, lexstore, working_set_index, working_set_runtime

NOTES = "Knowledge Base/Notes/Research"
CHECKLIST = f"{NOTES}/kelvane-intake-checklist.md"
BODY_ONLY = f"{NOTES}/quarry-shift-notes.md"
ONE_WORD = f"{NOTES}/tessaly-rollout.md"
PLAN = f"{NOTES}/vantry-window-plan.md"
RETRO = f"{NOTES}/vantry-window-retro.md"
HEAD = f"{NOTES}/harbin-cutover-plan.md"
OLDER = (f"{NOTES}/harbin-cutover-plan-v1.md", f"{NOTES}/harbin-cutover-plan-v2.md")


def _page(
    vault: Path, rel: str, title: str, unit: str, *, status: str = "active", extra: str = ""
) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: research-note\nstatus: {status}\nupdated: 2026-09-12\n{extra}---\n\n"
        f"# {title}\n\n## Summary\n\n{unit}\n",
        encoding="utf-8",
    )


@pytest.fixture
def titled_vault(vault: Path) -> Path:
    seed_ordinary_notes(vault)
    _page(
        vault,
        CHECKLIST,
        "Kelvane intake checklist",
        "- [decision] The kelvane intake seals are checked before each shift. ^k-1",
    )
    _page(
        vault,
        BODY_ONLY,
        "Quarry shift notes",
        "- [finding] The brennick ladle cracked on the second pour. ^b-1",
    )
    _page(
        vault,
        ONE_WORD,
        "Tessaly rollout",
        "- [decision] Tessaly went through the grinwald staging lane first. ^t-1",
    )
    _page(vault, PLAN, "Vantry window plan", "- [decision] The vantry window opens at six. ^v-1")
    _page(vault, RETRO, "Vantry window retro", "- [finding] The vantry window ran late twice. ^v-2")
    for index, rel in enumerate(OLDER, start=1):
        _page(
            vault,
            rel,
            "Harbin cutover plan",
            f"- [decision] Harbin cutover revision {index} moved the date. ^h-{index}",
            status="superseded",
            extra=f"superseded_by: [{HEAD}]\n",
        )
    _page(
        vault,
        HEAD,
        "Harbin cutover plan",
        "- [decision] The harbin cutover happens on the first weekend. ^h-3",
        extra=f"supersedes: [{OLDER[1]}]\n",
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


def _anchors(packet: dict) -> dict[str, tuple[str, list[str]]]:
    return {a["path"]: (a["status"], list(a["evidence"])) for a in packet.get("anchors") or ()}


def test_a_page_the_turn_names_by_its_title_resolves(titled_vault: Path) -> None:
    packet = commands.op_activate_context(
        titled_vault, turn="What's on the kelvane intake checklist?"
    )

    assert _anchors(packet) == {CHECKLIST: ("resolved", ["lexical_overlap", "retrieval"])}
    assert packet["abstained"] is False


def test_the_current_revision_alone_resolves(titled_vault: Path) -> None:
    packet = commands.op_activate_context(titled_vault, turn="What's the harbin cutover plan now?")

    assert _anchors(packet) == {HEAD: ("resolved", ["lexical_overlap", "retrieval"])}


@pytest.mark.parametrize(
    ("turn", "page"),
    [
        ("What happened with the brennick ladle?", BODY_ONLY),
        ("How did the grinwald staging go for tessaly?", ONE_WORD),
    ],
    ids=["body-phrase", "one-title-word"],
)
def test_a_page_not_named_by_its_title_stays_carried(
    titled_vault: Path, turn: str, page: str
) -> None:
    packet = commands.op_activate_context(titled_vault, turn=turn)

    assert _anchors(packet) == {page: ("retrieval_carried", ["retrieval"])}


def test_namesakes_still_ask(titled_vault: Path) -> None:
    packet = commands.op_activate_context(
        titled_vault, turn="Where did we land on the vantry window?"
    )

    assert packet["abstained"] is True
    assert {a["path"]: a["status"] for a in packet["anchors"]} == {
        PLAN: "retrieval_named",
        RETRO: "retrieval_named",
    }
