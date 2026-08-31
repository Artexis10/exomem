"""`bootstrap(profile="compact")` must actually be compact.

The profile existed but did almost nothing: compact was 64,070 bytes and full was
65,039 — a 1.5% saving — so every generic-MCP session start spent roughly 16,000
tokens of the caller's context before any work happened. The largest single cause was
shipping all six built-in packs' `agent_instructions` when only the *selected* pack's
guidance can ever apply.

These tests pin the saving so the profile cannot quietly collapse back into full.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import warnings

import pytest

from exomem import commands

#: Ceiling for the compact payload. Chosen above the measured floor so ordinary growth
#: is fine, and far below the 64 KB regression point. Lower it when compact shrinks
#: further; never raise it without deciding the extra bytes earn a caller's context.
#:
#: Raised once, from 56,000, and the decision is on the record with its arithmetic.
#: The epistemic contract added 3,198 bytes to a 52,877-byte floor, taking compact to
#: 56,075 — 75 bytes past the old ceiling. Those bytes earn their place, because the
#: payload is the entire contract a hosted or generic MCP client ever receives, and
#: without them such a client never learns that raw material is append-only, that a
#: changed conclusion is superseded rather than overwritten, or that a refuted claim
#: stays active. That doctrine reached only skill-capable Claude surfaces, so one vault
#: got two epistemologies depending on which client wrote to it.
#:
#: Fitting under 56,000 was possible and was declined on the merits: dropping the
#: `kinds` (193 B) and `relations` (145 B) sub-blocks of the payload's epistemic
#: vocabulary would have landed compact at 55,737, a real 263 bytes clear. They restate
#: material the payload carries elsewhere, and they were kept anyway, because an agent
#: reading the doctrine should not have to assemble the vocabulary from three other
#: sections to act on it.
#:
#: Be clear about what the raise is: this change spent the entire growth budget the old
#: ceiling expressed and pre-authorised 1,925 bytes more. It is not headroom restored.
#: A second addition of this size must argue for itself from scratch, and 58,000 still
#: sits ~6 KB below the 64,070-byte regression point the gate was built to catch.
#: `MINIMUM_SAVING_RATIO` below is untouched; the saving moved 32.74% -> 31.46%.
#:
#: Raised a second time, and the arithmetic is again on the record — re-measured
#: on the final tree rather than carried over from a draft. The measured floor at
#: 58,000 was 57,872 — 128 bytes of headroom, i.e. none. The due-state carriers add
#: three `authoring_contract.post_write` entries (`due_state`,
#: `due_state_handling`, `due_state_authority`) totalling 1,224 bytes, taking
#: compact to 59,096. The block those entries describe costs this measurement
#: nothing: it is vault-derived and absent on the empty fixture, and it is bounded
#: at five references anyway.
#:
#: Why the bytes earn their place. This payload is the ENTIRE contract a hookless
#: client receives, and the due-state block is a channel that arrives unasked on
#: ordinary results. An agent that receives counts it was never taught to read has
#: two failure modes and both are worse than the bytes: ignore them, and the change
#: delivers nothing; act on every one, and the substrate becomes the nag its own
#: design refuses to be. The three entries are the smallest statement of what the
#: counts are, when to raise one, and that the runtime never acts on them.
#:
#: Fitting under 58,000 was examined and rejected as dishonest rather than tight.
#: The only reductions available were dropping `due_state_authority` (183 B, the
#: line that says the runtime never resolves or archives anything on the counts'
#: behalf) or collapsing the handling guidance to its first clause (~250 B, losing
#: the fingerprint rule and the silence-beats-bureaucracy rule). Both remove
#: exactly the restraint the counters need in order not to become a nuisance.
#:
#: The new ceiling is 59,500: 404 bytes of headroom, deliberately less than the
#: last raise pre-authorised, and still ~4.5 KB below the 64,070-byte regression
#: point. `MINIMUM_SAVING_RATIO` is untouched, and the saving moved DOWN — 35.46%
#: without these entries to 34.98% with them — because bytes added to compact make
#: compact resemble full a little more. It stays far above the 15% floor. (The
#: 31.46% recorded for the previous raise is stale: the payload has moved since,
#: and the two figures here are both measured on the current tree, which is why
#: they do not chain onto it.)
#:
#: Raised a third time, measured on the final tree with the same method. The floor
#: at 59,500 measured 59,096 — exactly what the previous raise recorded, so nothing
#: else moved in between. Nag governance adds three `authoring_contract.post_write`
#: entries: `review_reason` (220 B), `family_disposition` (396 B) and
#: `family_disposition_reading` (290 B), 912 bytes with separators, taking compact
#: to 60,008. The dispositions and the ledger they describe cost this measurement
#: nothing: both are vault-derived and empty on the fixture.
#:
#: Why these bytes earn their place. The change gives a user a way to say "stop
#: suggesting this kind of thing" and gives the runtime a reason code to count. An
#: agent that has not been taught either does the two things this change exists to
#: prevent: it answers the request by lowering prominence, which silences every
#: family including the ones the user still wants, and it writes free-text `why`
#: strings that record `unspecified`, leaving the metrics with no denominator. A
#: hookless client receives this payload and nothing else, so untaught here is
#: untaught anywhere.
#:
#: Fitting under 59,500 was examined and rejected. It needed roughly 508 bytes,
#: which is two of the three entries. `review_reason` (220 B) is the vocabulary
#: itself; without it no code is ever composed. `family_disposition_reading`
#: (290 B) is the line saying a quiet family is silent rather than clean, which is
#: precisely the misreading a per-family silence introduces and the reason the
#: spec requires it. Collapsing `family_disposition` to the reference form alone
#: (~200 B) would drop "rather than lowering prominence" — the wrong answer the
#: guidance exists to displace.
#:
#: The new ceiling is 60,400: 392 bytes of headroom, again deliberately less than
#: the 404 the last raise left, and ~3.7 KB below the 64,070-byte regression point.
#: `MINIMUM_SAVING_RATIO` is untouched; the saving moved 35.63% -> 34.63%.
#:
#: Raised a FOURTH time, and this raise is different from the three above it:
#: it spends the whole budget the change that made it was allowed to spend, and
#: leaves a margin thinner than any previous raise. Read the arithmetic before
#: adding anything.
#:
#: Measured on this tree with the same method. The floor at 60,400 was 60,066 --
#: 334 bytes of headroom, already inside the warning band. Lifecycle routing adds
#: 1,240 bytes, taking compact to 61,306:
#:
#:   engagement          +258  the capture axis names the two lifecycle classes
#:                             (stated intent -> Planning, observed outcome ->
#:                             Records) and the pairing rule; the payload projects
#:                             the ACTIVE level's contract, so exactly one level's
#:                             text is ever counted here
#:   records             +560  `intent_boundary` gains `stated_intent`,
#:                             `observed_outcome` and `pairing_rule`;
#:                             `capture_examples` gains one paired landing
#:   simple_actions      +321  the `plan` front-door action, with its route
#:   planning            +193  the inventory form of `inspect` and the bounded
#:                             query that resolves an observation to one item
#:   common_actions        +8  `plan` in the action vocabulary
#:
#: Why the bytes earn their place. The evidence for these two classes exists ONLY
#: in the conversation, and only the CLI hooks can see a conversation -- so on a
#: hosted, claude.ai or ChatGPT client this payload is the entire mechanism. An
#: agent that is not taught them does exactly what the dogfood session recorded:
#: it treats "three done" and "the rest next time" as chat, files nothing, and
#: waits to be told to use Planning. The pairing rule is not decoration either:
#: without it the two classes produce a record and leave the plan item open,
#: which is the specific miss the whole change exists to close.
#:
#: The wording was cut twice before this number was accepted. The tentative-claim
#: and elapsed-time clause is stated once (in `intent_boundary`) rather than in
#: both carriers; the paired example is one clause rather than a sentence; the
#: Planning inventory and its resolving query are one key rather than two. Those
#: three passes removed 406 bytes. What remains is the rule and its two routes.
#:
#: The new ceiling is 61,400: 94 bytes of headroom, and that is a cliff, not a
#: budget. It is the cap the change was authorised to reach and it is now spent.
#: The next addition of any size trips this test, and the honest response is to
#: TRIM compact -- the queued compact-bootstrap trim -- not to raise this number
#: again. `MINIMUM_SAVING_RATIO` is untouched; compact still saves ~35% over full.
#:
#: That prediction came true on the next merge, and the response was the one
#: written above: TRIM, not raise. `main`'s vault-defined entity types added ~150
#: bytes of guidance on top of the lifecycle slice's 1,240, taking the merged
#: payload to 61,455 -- 55 over. 158 bytes came back out of the LIFECYCLE slice's
#: own text, because that is the text this branch is entitled to spend: the two
#: capture classes lost their preamble but not their routes; the pairing rule
#: lost "append the"/"the item"/"reported" but keeps one landing, two
#: consequences, the record-before-transition order, "once", and both named
#: non-outcomes; the paired example is a clause; the `plan` front-door row drops
#: one adjective. No rule left the payload, and the pins moved WITH the text
#: rather than being loosened around it. Merged compact is 61,297, 103 bytes
#: under. The ceiling did not move.
#:
#: TRIM again, same answer. Completing the action catalog so that every product
#: command is reachable from some action added ten names to `advanced` lists:
#: 183 bytes, taking the merged payload to 61,480 -- 80 over. The names are not
#: prose and there was nothing in them to shorten; a name removed is a command
#: an agent can no longer route to, which is the defect being fixed.
#:
#: The bytes came back from redundancy instead. Six of the entries named a
#: command that is already another action's primary route -- `connect_memory`
#: and `review_memory` under `ask`, `connect_memory` and `plan_memory` under
#: `remember`, `review_memory` under `review`, `maintain_memory` under
#: `maintain`. The catalog already names `connect`, `review`, `plan` and
#: `maintain` as actions, so those entries told an agent nothing the payload
#: did not already say, and `plan` only became an action on the merge that
#: caused the overflow. The rule is now: `advanced` carries commands no route
#: reaches. Coverage is unchanged -- the gate counts routes and `advanced`
#: together -- and compact is 61,376, 24 bytes under. The ceiling did not move.
#:
#: 24 bytes is not headroom. The next addition trims or argues, and this branch
#: has no claim on the argument: it spent 79 of the 103 bytes main left.
#:
#: TRIMMED, and this time the trim is the whole change rather than the price of
#: one. The pre-write destination-choice clause landed in the FULL contract only
#: and the canonical spec recorded the hook in as many words -- "the compact
#: payload remains byte-identical until the queued compact-bootstrap trim admits
#: the clause" -- with a test pinning that absence so it could not drift. This is
#: that trim. The clause is now carried by every profile, and the ceiling did not
#: move.
#:
#: Method is the one the two entries above set, and nothing here departs from it:
#: bytes come back from REDUNDANCY -- text the payload already states somewhere
#: else -- and pins moved WITH their text rather than being loosened around it.
#: Six passages, 864 bytes, each measured on the final tree by restoring it alone:
#:
#:   workflow.loop suggest-links step       105  the same call is
#:                                               `authoring_contract.canonical_loop`
#:                                               step 5 on the draft, and
#:                                               `preflight.connect_memory` names it
#:                                               as the standard pre-write check
#:   workflow.loop write-routing step       141  the four routes it listed ARE
#:                                               `authoring_contract.route_by_intent`,
#:                                               and each of the four is separately
#:                                               pinned there
#:   three retry_examples                   127  synonyms, adjacent terms and
#:                                               scope='vault' are `workflow.miss_rule`,
#:                                               which states all three as the rule
#:                                               rather than as examples of it. One
#:                                               fragment went with them that miss_rule
#:                                               does NOT restate -- see below
#:   one retry_example                       77  deep=true for synthesis is
#:                                               canonical_loop step 2 and
#:                                               `tool_defaults.reasoning_lookup`
#:   tool_defaults.metadata_lookup          156  the same tool with byte-identical args
#:                                               as `normal_lookup`; the richer filters
#:                                               it pointed at are spelled out in
#:                                               `search_guidance.semantic_recall`
#:   performance_profiles normal, reasoning 258  each repeated one `tool_defaults`
#:                                               row's args. `normal` also restated
#:                                               that row's `when`; `reasoning` did
#:                                               not, because that row carried no
#:                                               `when` at all -- its interpretation
#:                                               survives in canonical_loop step 2,
#:                                               and this delivery ADDS the missing
#:                                               `when` to the row itself so the
#:                                               spec's three-lookup scenario still
#:                                               reads all three from `tool_defaults`
#:                                               (+42 B). The diagnostics profile stays
#:
#: One fragment left the payload UNRESTATED, and the ruling is deliberate rather
#: than an oversight. "try synonyms and singular/plural forms" went out with the
#: three retry examples, and miss_rule covers "synonyms" but not the morphological
#: half: a plural is not a synonym. It is dropped as a sub-case of the synonym
#: retry tactic, not as a rule -- the shipped skill scaffold still teaches
#: morphological retry in references/operations.md, so the tactic is not lost to
#: the product, only to this payload. Restoring it means widening miss_rule, which
#: costs bytes this change does not have; it is the FIRST candidate to restore when
#: a future trim frees them.
#:
#: What did NOT leave: no rule, no landing, no consequence, no named non-outcome,
#: no route, and no command name reachable nowhere else. connect_memory, remember,
#: replace_memory, observe_memory and edit_memory all keep their routes. The fifth
#: retry example survives on the same test: scan-only BEFORE proposing a migration
#: or copy is a guard, and nothing else in the payload states it.
#:
#: The clause costs compact 316 bytes in a condensed wording, 290 characters
#: against full's 512, and both halves of the rule survive the condensation --
#: destination choice happens at write time, by finding a focused existing
#: destination or creating one; the post-write advisory is the safety net for
#: missed routing, never the primary mechanism. Full's wording is untouched.
#:
#: Two of the cuts left a seam, and closing them is part of the same delivery
#: rather than a later patch: the reasoning row's missing `when` above (+42 B),
#: and the loop's last step, which said "read the returned warnings" after the
#: step naming the write had gone. It now opens "after a write" (+15 B), which
#: supplies the antecedent the cut removed.
#:
#: 61,376 - 864 + 316 + 57 = 60,885, measured. That is 515 bytes of headroom and
#: the first time since the fourth raise that this payload has sat clear of
#: HEADROOM_WARNING_BYTES. The margin is load-bearing in both directions:
#: restoring any ONE of the six passages while keeping the clause puts compact
#: back inside the warning band -- 410, 374, 388, 438, 359 and 257 bytes of
#: headroom respectively -- so nothing here was cut for margin that was not
#: needed. `MINIMUM_SAVING_RATIO` is untouched; the saving moved 35.32% -> 35.29%,
#: because the redundant passages were shared text and full lost them too.
#:
#: Two larger redundancies were found and NOT taken. They are recorded here so
#: the next trim starts from them instead of rediscovering them.
#: `authoring_contract.semantic_units.contract` is the top-level
#: `semantic_authoring` projection repeated in full -- 9,042 bytes, the single
#: largest duplication in this payload -- and `tool_catalog` is `product_commands`
#: repeated, 1,602 more. Neither is prose. Both are published payload KEYS a
#: client may read, and the nested projection is pinned by name in
#: test_bootstrap.py, so taking either means deleting a pin or withdrawing a key:
#: a surface decision, not a trim. `records.software_rule` was rejected on a
#: different ground -- it does restate `planning.execution_truth_boundary`, but it
#: is that boundary stated INSIDE the Records contract, and it is pinned there.
#:
#: 515 bytes is spendable budget, not licence. The next addition of this size
#: argues for itself the way the raises above had to.
COMPACT_BYTE_CEILING = 61_400

#: The defect was compact and full being near-identical. A profile that does not
#: measurably differ from full is not a profile.
MINIMUM_SAVING_RATIO = 0.15


@pytest.fixture(scope="module")
def payloads() -> dict[str, dict]:
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "Knowledge Base").mkdir()
    return {
        profile: commands.op_bootstrap(root, profile=profile)
        for profile in ("compact", "full", "diagnostics")
    }


def _size(payload: dict) -> int:
    return len(json.dumps(payload))


#: Headroom below which the ceiling stops being a budget and becomes a cliff.
#: Warn rather than fail: the remaining bytes are still legitimately spendable,
#: and turning "nearly full" into a failure would just be the ceiling moved down
#: without the argument the ceiling's own docstring demands.
HEADROOM_WARNING_BYTES = 512


def test_compact_stays_under_its_byte_ceiling(payloads):
    size = _size(payloads["compact"])
    assert size <= COMPACT_BYTE_CEILING, (
        f"compact bootstrap is {size:,} bytes (~{size // 4:,} tokens), over the "
        f"{COMPACT_BYTE_CEILING:,} ceiling by {size - COMPACT_BYTE_CEILING:,}"
    )
    headroom = COMPACT_BYTE_CEILING - size
    if headroom < HEADROOM_WARNING_BYTES:
        # The failure mode this catches is not the ceiling being wrong, it is
        # the ceiling being reached *silently*. Compact grew to 70 bytes of
        # headroom and nobody knew until an unrelated release PR went red two
        # merges later, which is a bad place to first read the argument for why
        # the number is what it is.
        warnings.warn(
            f"compact bootstrap is {size:,} bytes with only {headroom:,} bytes "
            f"under the {COMPACT_BYTE_CEILING:,} ceiling. The next addition of "
            "any size will trip it. Either trim compact, or raise the ceiling "
            "with the reasoning the constant's docstring requires -- but decide "
            "it deliberately rather than discovering it as a red CI run.",
            stacklevel=2,
        )


def test_compact_clears_the_warning_headroom(payloads):
    assert COMPACT_BYTE_CEILING - _size(payloads["compact"]) >= HEADROOM_WARNING_BYTES


def test_compact_is_materially_smaller_than_full(payloads):
    compact, full = _size(payloads["compact"]), _size(payloads["full"])
    saving = (full - compact) / full
    assert saving >= MINIMUM_SAVING_RATIO, (
        f"compact saves only {saving:.1%} over full ({compact:,} vs {full:,}); the "
        "profile has collapsed back into full"
    )


def test_profiles_are_ordered_by_size(payloads):
    assert _size(payloads["compact"]) < _size(payloads["full"]) <= _size(
        payloads["diagnostics"]
    )


# ------------------------------------------------------------------ what was trimmed


def test_compact_omits_unselected_pack_guidance(payloads):
    """Only the selected pack's instructions can apply; the rest are dead weight."""
    available = payloads["compact"]["knowledge_packs"]["available"]
    assert available, "the catalogue must still be discoverable"
    for pack in available:
        assert "agent_instructions" not in pack
        assert "examples" not in pack


def test_compact_still_names_every_pack(payloads):
    """Trimming bodies must not hide which packs exist."""
    compact_ids = {p["id"] for p in payloads["compact"]["knowledge_packs"]["available"]}
    full_ids = {p["id"] for p in payloads["full"]["knowledge_packs"]["available"]}
    assert compact_ids == full_ids
    for pack in payloads["compact"]["knowledge_packs"]["available"]:
        assert pack["name"]


def test_full_retains_the_complete_catalogue(payloads):
    assert any(
        "agent_instructions" in pack
        for pack in payloads["full"]["knowledge_packs"]["available"]
    )


# --------------------------------------------------------------- what must survive


def test_selected_pack_guidance_survives_in_compact(payloads):
    """The one pack whose instructions actually apply must keep them."""
    selected = json.dumps(payloads["compact"]["knowledge_packs"]["selected"])
    assert "agent_instructions" in selected


def test_compact_action_catalogues_reference_selected_pack_guidance_once(payloads):
    """Action aliases point at the selected pack; they do not repeat its body."""
    compact = payloads["compact"]
    for catalogue_name in ("simple_actions", "front_door_actions"):
        for action in compact[catalogue_name].values():
            for guidance in action.get("selected_pack_guidance", []):
                assert set(guidance) <= {"pack_id", "name"}

    assert any(
        "agent_instructions" in guidance
        for action in payloads["full"]["simple_actions"].values()
        for guidance in action.get("selected_pack_guidance", [])
    )


def test_compact_still_teaches_the_core_loop(payloads):
    """A smaller contract is only a win if it is still a contract."""
    compact = payloads["compact"]
    workflow = compact["workflow"]
    assert workflow["save_rule"]
    assert workflow["miss_rule"]
    for section in ("server", "active_capabilities", "governance", "search_guidance"):
        assert section in compact, section


def test_bootstrap_planning_contract_is_complete_and_exact(payloads):
    planning = payloads["full"]["planning"]

    assert planning["route"] == {
        "tool": "plan_memory",
        "actions": ["inspect", "create", "query", "add", "update", "triage"],
    }
    assert planning["kinds"] == ["area", "outcome", "initiative", "work-item"]
    assert planning["horizons"] == ["inbox", "week", "month", "quarter", "year", "multi-year"]
    assert planning["lifecycle"] == ["active", "archived"]
    assert planning["priorities"] == ["critical", "high", "medium", "low", "none"]
    assert planning["commitments"] == ["uncommitted", "considering", "committed"]
    for key in (
        "default_capture",
        "manual_first",
        "template_independence",
        "horizon_semantics",
        "intent_first_routing",
        "evidence_execution_boundary",
        "execution_truth_boundary",
    ):
        assert planning[key]


def test_compact_and_full_agree_on_everything_but_detail(payloads):
    """The trim is a presentation choice; it must not change what is advertised."""
    compact, full = payloads["compact"], payloads["full"]
    assert set(compact) <= set(full)
    assert compact["server"] == full["server"]
    assert compact["active_capabilities"] == full["active_capabilities"]
