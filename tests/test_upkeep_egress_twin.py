"""F1: an upkeep item whose subject is withheld is indistinguishable from an absent one.

Through the one dispatcher every door shares, as a non-owner principal under a
governed policy: `review_memory(mode="item")`, `review_item_context` and
`triage_memory` answer a withheld item exactly as they answer an id that does
not exist, in code and body, with any fingerprint or none, and both stop before
any revalidation (the same timing class). Triage of a withheld item mutates
nothing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import dreamer_fixture as fx
import pytest
from test_governance_egress import SCOPE_ID, _external, write_rule

from exomem import commands, dreamer, dreamer_families, dreamer_store, freshness, review_state
from exomem import upkeep
from exomem.governance import egress, membership, policy
from exomem.governance.principal import request_scope
from exomem.writer_lease import invoke_command

LATER = time.time() + 3 * 3600
ABSENT = upkeep.upkeep_ref("0123456789abcdef01234567")


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _cmd(name: str):
    return next(c for c in commands.PRODUCT_COMMANDS if c.name == name)


def _withhold(vault: Path, rel: str) -> None:
    target = vault / "Knowledge Base" / "_Governance" / "scopes" / "patterns.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Withheld\n"
        f'paths: ["{rel.removeprefix("Knowledge Base/")}"]\n',
        encoding="utf-8",
    )
    write_rule(vault, ceiling=0)
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _outcome(call, ref: str) -> tuple[str, str]:
    try:
        return ("ok", json.dumps(call(), sort_keys=True, default=str).replace(ref, "<ref>"))
    except Exception as error:  # noqa: BLE001 - the error IS the observable
        return (type(error).__name__, str(error).replace(ref, "<ref>"))


def _doors(vault: Path, ref: str, fingerprint: str | None) -> dict[str, object]:
    extra = {} if fingerprint is None else {"expected_fingerprint": fingerprint}
    return {
        "item": lambda: invoke_command(_cmd("review_memory"), vault, mode="item", ref=ref, **extra),
        "context": lambda: invoke_command(_cmd("review_item_context"), vault, ref=ref, **extra),
        "triage": lambda: invoke_command(
            _cmd("triage_memory"),
            vault,
            ref=ref,
            action="dismiss",
            why="false_positive: probe",
            **extra,
        ),
    }


def test_a_withheld_item_answers_exactly_like_an_absent_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = fx.build(tmp_path)
    for now in (None, LATER):
        results = fx.run_to_quiet(vault, now=now)
        assert all(result.stop_reason != "error" for result in results), results
    # Computable from a guessed path alone: an oracle must not answer it.
    ref = upkeep.upkeep_ref(
        dreamer_store.candidate_id(dreamer_families.HYDRATION_KIND, fx.ENTITY, "")
    )
    real = upkeep.item(vault, ref)["item"]["fingerprint"]
    _withhold(vault, fx.ENTITY)
    revalidated: list[str] = []
    real_propose = dreamer_families.propose
    monkeypatch.setattr(
        dreamer_families,
        "propose",
        lambda ctx, row: revalidated.append(row["id"]) or real_propose(ctx, row),
    )
    before = json.dumps(review_state.ReviewStateStore(vault).load(), sort_keys=True)
    with request_scope(_external()):
        for fingerprint in (None, "0" * 24, real):
            withheld = {
                door: _outcome(call, ref) for door, call in _doors(vault, ref, fingerprint).items()
            }
            absent = {
                door: _outcome(call, ABSENT)
                for door, call in _doors(vault, ABSENT, fingerprint).items()
            }
            assert withheld == absent, (fingerprint, withheld, absent)
            assert all(outcome[0] != "ok" for outcome in withheld.values()), withheld
            assert "REVIEW_ITEM_NOT_FOUND" in withheld["context"][1]
            assert "REVIEW_ITEM_NOT_FOUND" in withheld["triage"][1]
    assert revalidated == []
    after = json.dumps(review_state.ReviewStateStore(vault).load(), sort_keys=True)
    assert after == before
