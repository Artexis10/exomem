"""Per-item egress for alias and convention upkeep: a withheld page equals an absent one.

Each case builds two vaults under the same governed policy. In the first, some
pages are withheld from an external caller; in the second they do not exist.
Everything that caller can observe through item, context, triage, the explicit
list and the activation carrier must be identical, including the served
fingerprint and every count.

The member-bound case pins the ruling on design §8 rule 4: a fold key carried
by more than 32 pages is not served, but only pages the caller may see count
toward that bound. Counting withheld pages would let the caller infer that
withheld pages carry the term.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import dreamer_fixture as fx
import pytest
from test_governance_egress import SCOPE_ID, _external, write_rule

from exomem import commands, dreamer, dreamer_families, dreamer_store, freshness, upkeep
from exomem.governance import egress, membership, policy
from exomem.governance.principal import owner_principal, request_scope
from exomem.writer_lease import invoke_command

LATER = time.time() + 3 * 3600
#: Sorts before every visible note, so withheld rows come first in each key.
RESTRICTED = f"{fx.KB}/Notes/A-Restricted"


def _alias_ref(subject: str, key: str) -> str:
    return upkeep.upkeep_ref(dreamer_store.candidate_id("anchor.alias", subject, key))


ALIAS_REF = _alias_ref(fx.ENTITY, fx.VARIANT_KEY)
TAG_REF = upkeep.upkeep_ref(dreamer_store.candidate_id("convention.tag", "", fx.TAG_KEY))
REFS = {"alias": ALIAS_REF, "tag": TAG_REF}


def _reset() -> None:
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    upkeep.reset_delivery_state()
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


@pytest.fixture(autouse=True)
def _clean():
    _reset()
    yield
    _reset()


class _Clock:
    def __init__(self) -> None:
        self.mono = 1_000_000.0
        self.wall = LATER

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


def _rival() -> dict[str, str]:
    """A second page that carries the variant's fold key as a name."""
    text = (
        fx.entity()
        .replace("title: Orbit Pump", "title: Orbit Pump Spare")
        .replace("updated: 2026-01-10\n", 'updated: 2026-01-10\naliases:\n  - "orbit pump"\n')
    )
    return {f"{RESTRICTED}/orbit-pump-spare.md": text}


def _majority() -> dict[str, str]:
    """Two withheld notes that make `Pump Care` the majority for the owner."""
    return {
        f"{RESTRICTED}/pump-care-{index}.md": fx.note(
            f"Restricted care {index}", tags=(fx.TAG_MINORITY,)
        )
        for index in ("a", "b")
    }


def _crowd() -> dict[str, str]:
    """33 withheld notes that carry the tag and link the page by its title.

    Past the member bound, and ahead of every visible member in path order:
    the owner is served neither item, the caller exactly what the absent twin
    serves. Their links resolve, so they are no variant, yet they share the
    fold key and sort before the visible referrers.
    """
    return {
        f"{RESTRICTED}/crowd-{index:02d}.md": fx.note(
            f"Restricted crowd {index:02d}",
            tags=(fx.TAG_MAJORITY,),
            links="Service the [[Orbit Pump]] weekly.",
        )
        for index in range(33)
    }


def _carriers() -> dict[str, str]:
    """33 withheld pages that carry the entity's name, ahead of it in path order."""
    return {f"{RESTRICTED}/orbit-pump-{index:02d}.md": fx.note("Orbit Pump") for index in range(33)}


#: A visible page that shares the entity's title: the variant links are then an
#: ambiguity, reported under the audit category that owns the link.
_VISIBLE_TWIN = {f"{fx.KB}/Notes/Insights/orbit-pump-copy.md": fx.note("Orbit Pump")}


def _edit_withheld(vault: Path, withheld: bool) -> None:
    """Edit only a withheld member's body; its tag is unchanged."""
    if withheld:
        rel = f"{RESTRICTED}/pump-care-a.md"
        fx.edit(
            vault, rel, fx.note("Restricted care a", tags=(fx.TAG_MINORITY,), observation="New.")
        )
    results = fx.run_to_quiet(vault, now=LATER + 7200)
    assert all(result.stop_reason != "error" for result in results), results


GLOBS = ["Notes/A-Restricted/**"]
CASES = {
    "competing_name": {"extra": _rival},
    "majority_spelling": {"extra": _majority},
    "member_bound": {"extra": _crowd},
    "name_carriers": {"extra": _carriers},
    "integrity": {
        "extra": lambda: {f"{RESTRICTED}/orbit-pump-00.md": fx.note("Orbit Pump")},
        "shared": _VISIBLE_TWIN,
        "served": False,
    },
    "withheld_edit": {"extra": _majority, "after": _edit_withheld},
    "subject": {
        "extra": dict,
        "globs": [*GLOBS, "Notes/Entities/orbit-pump.md"],
        "absent": (fx.ENTITY,),
        "served": False,
    },
}


def _govern(vault: Path, globs: list[str]) -> None:
    target = vault / fx.KB / "_Governance" / "scopes" / "patterns.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    listed = ", ".join(f'"{glob}"' for glob in globs)
    target.write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Withheld\npaths: [{listed}]\n",
        encoding="utf-8",
    )
    write_rule(vault, ceiling=0)


def _build(root: Path, *, extra: dict[str, str], globs: list[str], absent=()) -> Path:
    vault = fx.build_vocabulary(root, with_graph=False)
    for rel, text in extra.items():
        fx.write(vault, rel, text)
    for rel in absent:
        (vault / rel).unlink()
    _govern(vault, globs)
    freshness.clear()
    fx.seed(vault)
    fx.publish_graph(vault)
    for now in (None, LATER):
        results = fx.run_to_quiet(vault, now=now)
        assert all(result.stop_reason != "error" for result in results), results
    _reset_caches()
    return vault


def _reset_caches() -> None:
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    upkeep.reset_delivery_state()
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _cmd(name: str):
    return next(c for c in commands.PRODUCT_COMMANDS if c.name == name)


def _scrub(value: object) -> object:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in {"updated_at", "setting"}}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _outcome(call) -> tuple[str, str]:
    try:
        return ("ok", json.dumps(_scrub(call()), sort_keys=True, default=str))
    except Exception as error:  # noqa: BLE001 - the error IS the observable
        return (type(error).__name__, str(error))


def _carry(
    vault: Path, clock: _Clock, sessions: int, tag: str, recent: tuple[str, ...] = ()
) -> list[object]:
    out = []
    for index in range(sessions):
        clock.advance(11 * 60)
        packet = {
            "recent_context": [{"path": path, "title": "t", "why": "edited"} for path in recent],
            "anchors": [],
            "budget": {"limit_chars": 4000, "used_chars": 0},
            "abstention": {"reason": "unresolved"},
        }
        upkeep.for_packet(vault, packet, session=f"{tag}-{index}")
        out.append(_scrub(packet.get("upkeep")))
    return out


def _observe(vault: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Everything an external caller can see, in a fixed order of calls."""
    # Building the other twin cleared this vault's freshness map: seed it again,
    # as the running service's watcher keeps it.
    freshness.clear()
    fx.seed(vault)
    _reset_caches()
    clock = _Clock()
    monkeypatch.setattr(upkeep, "_monotonic", lambda: clock.mono)
    monkeypatch.setattr(upkeep, "_wall", lambda: clock.wall)
    monkeypatch.setattr(dreamer, "delivering", lambda: True)
    seen: dict[str, object] = {}
    with request_scope(_external()):
        seen["review"] = _outcome(lambda: upkeep.review(vault, limit=50))
        for name, ref in REFS.items():
            seen[f"item:{name}"] = _outcome(
                lambda ref=ref: invoke_command(_cmd("review_memory"), vault, mode="item", ref=ref)
            )
            seen[f"context:{name}"] = _outcome(
                lambda ref=ref: invoke_command(_cmd("review_item_context"), vault, ref=ref)
            )
        # A recently edited visible member is offered first, whatever is withheld.
        seen["carrier_recent"] = _carry(vault, clock, 1, "recent", recent=(fx.TAG_TWO,))
        seen["carrier"] = _carry(vault, clock, 4, "before")
        for name, ref in REFS.items():
            seen[f"triage:{name}"] = _outcome(
                lambda ref=ref: invoke_command(
                    _cmd("triage_memory"), vault, ref=ref, action="dismiss", why="handled: probe"
                )
            )
        clock.advance(8 * 86400)
        seen["carrier_after"] = _carry(vault, clock, 3, "after")
        seen["review_after"] = _outcome(lambda: upkeep.review(vault, state="all", limit=50))
    return seen


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_withheld_member_equals_an_absent_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    spec = CASES[case]
    globs = spec.get("globs", GLOBS)
    shared = dict(spec.get("shared", {}))
    withheld = _build(tmp_path / "withheld", extra={**shared, **spec["extra"]()}, globs=globs)
    absent = _build(tmp_path / "absent", extra=shared, globs=globs, absent=spec.get("absent", ()))
    if "after" in spec:
        spec["after"](withheld, True)
        spec["after"](absent, False)
    left = _observe(withheld, monkeypatch)
    right = _observe(absent, monkeypatch)
    assert left == right
    if spec.get("served", True):
        # Not a vacuous equality: the caller really is served both items.
        assert right["item:alias"][0] == "ok", right["item:alias"]
        assert right["item:tag"][0] == "ok", right["item:tag"]
        assert any(block and block.get("items") for block in right["carrier"]), right["carrier"]
    elif case == "integrity":
        assert '"forward_reference": 1' in right["review"][1], right["review"]
    else:
        assert "REVIEW_ITEM_NOT_FOUND" in right["item:alias"][1]


def test_the_owner_counts_every_member_toward_the_bound(tmp_path: Path) -> None:
    """The member bound does bite for a caller who sees the crowd."""
    vault = _build(tmp_path, extra=_crowd(), globs=GLOBS)
    with request_scope(owner_principal()):
        kinds = {item["kind"] for item in upkeep.review(vault, limit=50)["items"]}
    assert "anchor.alias" not in kinds
    assert "convention.tag" not in kinds
    assert "convention.category" in kinds


def test_the_owner_and_the_caller_see_different_minorities(tmp_path: Path) -> None:
    vault = _build(tmp_path, extra=_majority(), globs=GLOBS)
    with request_scope(owner_principal()):
        owner = next(
            item
            for item in upkeep.review(vault, limit=50)["items"]
            if item["kind"] == "convention.tag"
        )
    assert owner["subject"]["title"] == "Pump care log"
    stored = next(
        row
        for row in dreamer_store.read_view(vault).candidates
        if row["family"] == dreamer_families.CONVENTION_FAMILY and row["kind"] == "convention.tag"
    )
    # The owner's view takes the stored fingerprint unchanged.
    assert owner["fingerprint"] == stored["fingerprint"]
    _reset_caches()
    with request_scope(_external()):
        caller = next(
            item
            for item in upkeep.review(vault, limit=50)["items"]
            if item["kind"] == "convention.tag"
        )
    assert caller["subject"]["title"] == "Pump care checklist"
    assert caller["fingerprint"] != owner["fingerprint"]


OLFILTER = f"{fx.KB}/Notes/Entities/olfilter.md"
OLFILTER_NOTE = f"{fx.KB}/Notes/Insights/olfilter-check.md"


def test_a_non_ascii_crowd_of_resolving_links_hides_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spellings compare casefolded: `ÖLFILTER` names `Ölfilter`, `Ölfilters` does not.

    33 withheld notes link the page by its own title in capitals, ahead of the
    one visible note that links a variant. The caller is served the variant
    exactly as on a vault without the crowd.
    """
    shared = {
        OLFILTER: fx.entity().replace("title: Orbit Pump", "title: Ölfilter"),
        OLFILTER_NOTE: fx.note("Oil filter check", links="Check the [[Ölfilters]] monthly."),
    }
    crowd = {
        f"{RESTRICTED}/ol-{index:02d}.md": fx.note(
            f"Crowd {index:02d}", links="Swap the [[ÖLFILTER]] yearly."
        )
        for index in range(33)
    }
    withheld = _build(tmp_path / "withheld", extra={**shared, **crowd}, globs=GLOBS)
    absent = _build(tmp_path / "absent", extra=shared, globs=GLOBS)
    ref = _alias_ref(OLFILTER, "ölfilter")

    def observe(vault: Path) -> dict[str, object]:
        freshness.clear()
        fx.seed(vault)
        _reset_caches()
        with request_scope(_external()):
            return {
                "review": _outcome(lambda: upkeep.review(vault, limit=50)),
                "item": _outcome(
                    lambda: invoke_command(_cmd("review_memory"), vault, mode="item", ref=ref)
                ),
            }

    left, right = observe(withheld), observe(absent)
    assert left == right
    assert right["item"][0] == "ok", right["item"]
    assert "Ölfilters" in right["item"][1]
