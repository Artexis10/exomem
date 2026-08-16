## Context

`op_bootstrap` returns the only operating contract a non-skill client ever sees. The
shipped `SKILL.md` scaffold carries the product's epistemology, but it reaches only
skill-capable Claude surfaces. Everything else — hosted agents, generic MCP clients —
gets the payload and nothing more.

Three facts constrain the shape of the fix.

First, `_filter_bootstrap_payload` walks the whole payload and deletes any string that
names a command the active surface cannot call. That is correct for routing advice and
catastrophic for doctrine: a commitment phrased as "supersede with `replace_memory`"
would vanish on precisely the reduced surfaces that most need to be told to supersede.

Second, `tests/test_bootstrap_compact_budget.py` pins the compact payload at 56,000
bytes and it currently measures 52,877. There are roughly three kilobytes of headroom.
The audit that motivated this change bounded the whole addition at about fifty lines,
which is the same constraint arriving from the other direction.

Third, the vocabulary already exists in code. `semantic_units.EPISTEMIC_OUTCOMES`,
`semantic_units.GOVERNED_UNIT_METADATA_KEYS`, and `semantic_blocks.BLOCK_TYPES` are the
single source of the words. Any doctrine text that restates them by hand becomes a
second, drifting definition of the thing.

## Goals / Non-Goals

Goals:

- Put the five commitments and the loop vocabulary on the path every client tier reads,
  including the compact profile.
- Make the taught vocabulary identical to the shipped vocabulary, and provable to be so.
- Keep the doctrine intact on a reduced surface.
- Write the doctrine dense enough that the compact byte budget is a real constraint on
  its wording rather than a reason to demote it out of compact.

Non-Goals:

- Per-vault due-state counts in the payload. Deferred; see Decisions.
- Any change to the pinned MCP tool surface, tool docstrings, or schemas.
- Any bump of `semantic_authoring.AUTHORING_CONTRACT_VERSION`.
- Any new tool, argument, filter, index, or ranking behaviour. This change is
  instruction placement, not capability.
- Restating the full scaffold. The scaffold stays the long form; the payload carries the
  commitments and the vocabulary, not the worked examples.

## Decisions

**The doctrine is a top-level payload section, not an extension of `authoring_contract`.**
`authoring_contract` is the write loop: how to draft, preflight, write, and follow up.
The commitments govern what may be written down at all and how a claim is allowed to
change over time, and they apply to reading as much as to writing. Nesting them under a
write loop would also hand them a section whose neighbouring strings are dense with tool
names, raising the odds a future edit phrases a commitment in routable terms. A sibling
section named for the discipline is also the thing a client can grep for, which is the
literal failure this change fixes.

**Commitments name no tool, and the section carries no routing of its own.**
`_filter_bootstrap_payload` deletes strings naming unavailable commands, so a commitment
phrased as a tool call would vanish on exactly the reduced surfaces that most need to be
told to supersede rather than overwrite. The five commitments therefore name no tool.
This mirrors the existing comment on the `engagement` contract, which is deliberately
tool-agnostic for the same reason, and a test asserts the commitments survive a surface
exporting almost nothing.

The first draft carried a sibling `routes` mapping so the tool advice could be dropped
per-key without touching a commitment. It was removed: `tool_defaults.supersede`,
`tool_defaults.mutate_semantic_unit`, and `authoring_contract.route_by_intent` already
route these intents, and a second copy would be a second thing to drift, paid for out of
a byte budget that turned out to be the binding constraint. The doctrine says what must
be true; the existing routing sections say which call makes it true.

**The vocabulary is derived from the modules that own it, not retyped.**
The payload builds its outcome list from `semantic_units.EPISTEMIC_OUTCOMES` and its
metadata-key list from `semantic_units.GOVERNED_UNIT_METADATA_KEYS`. If a sixth outcome
is ever added, bootstrap teaches it the same day rather than a release later. Tests
assert equality against those constants rather than against literals, so a hand-edited
divergence fails.

**Carried on compact, not gated to `full`.**
A generic client calls `bootstrap` with defaults. Doctrine shipped only on `full` is
doctrine shipped to nobody in the tier that lacks it. The section is written dense —
one line per commitment, one line per vocabulary term — to fit the compact budget
rather than being demoted out of it.

**`contract_version` moves; `AUTHORING_CONTRACT_VERSION` does not.**
The bootstrap contract has an established rule, spelled out in the governance
requirement of `agent-bootstrap-contract`, that adding a section moves the contract
version. This adds a section, so it moves, from `2026-08-11.1` to `2026-08-16.1`. That
constant is pinned in exactly one test and in no shipped artifact.

`AUTHORING_CONTRACT_VERSION` is a different question and the answer is no. That version
and its content digest are projected verbatim into five pinned MCP tool descriptions,
roughly twenty shipped `SKILL.md` files, `docs/capabilities.md`, `docs/semantic-language.md`,
and two pinned test constants. `add-epistemic-loop-primitives` deferred that bump
deliberately and said a later change may pay it when the contract genuinely needs to
*teach* the loop primitives. This change does not need it: the doctrine is bootstrap
guidance, which is exactly the payload's own job, and nothing here requires the
normative authoring contract to restate the vocabulary. Paying a twenty-file regeneration
to relocate three sentences would be a worse trade than the one already declined.

**Deferred by design: per-vault due-state counts.**
The audit also asks the payload to report deterministic per-vault due state — "2
predictions past their check date; 1 unfinished experiment" — so a session opens knowing
what it owes. That is not implemented here, and the reason is not effort. The predicate
for "due" and "unfinished" is being defined right now by the epistemic review and audit
category work in a parallel lane that has not merged. Two independent definitions of the
same predicate is precisely the drift this change exists to remove, and the payload
version of it would be the one users see. Implementing a private predicate here would
also make the parallel lane's merge a behaviour change to the public contract rather
than an addition to it.

The extension point is deliberate and narrow: `epistemic_contract` is a plain dictionary
assembled from vault-independent constants, so the counts land as one additional
vault-derived key alongside the existing vault-independent ones, computed from the
audit categories once they exist. No key currently in the section needs to move to make
room, and the section's public shape does not change when it arrives. A comment in
`op_bootstrap` marks the spot and names the blocking dependency.

**Recipes describe blocks, not new page types.**
`note_type_recipes` currently lists page types. `question`, `hypothesis`, and
`prediction` are semantic-unit kinds authored inside a compiled page. Each new recipe
says so in its first clause, so an agent reading the list cannot conclude that
`remember(note_type="prediction")` is a thing. The alternative — a separate
`unit_recipes` section — was rejected because an agent looking for "how do I write down
a question" looks in the recipes list, and a second list is a second place to miss.

**One supplementary edit to `intent_boundary`, and none to `capture_examples`.**
The Records `intent_boundary` distinguishes observed state from intended future state.
A checkable claim about a future observation is neither, and without a third line an
agent routes it into whichever of the two it resembles most. That is a genuine routing
gap and one line closes it. `capture_examples` was considered and left alone: it is
about resolving a compatible Records collection before appending, and a prediction is
not a collection entry. The capture nudge belongs in `epistemic_contract`, which — unlike
the whole `records` section — survives on a surface that does not export Records.

## Risks / Trade-offs

**The compact byte ceiling is raised, from 56,000 to 58,000.** This was not the plan and
is worth stating plainly, with the arithmetic, because a gate relaxation justified by
wrong numbers is a worse precedent than the two kilobytes.

The measurements, taken by extracting `origin/main` @ 64475616 with `git archive` and
importing that tree ahead of the working copy: the pre-change compact payload is 52,877
bytes; the doctrine adds 3,198; compact lands at 56,075, which is **75 bytes past** the
old 56,000 ceiling. The compact-versus-full saving moves from 32.74% to 31.46%, against
a 15% `MINIMUM_SAVING_RATIO` floor that is untouched.

**A cheaper option existed and was declined on the merits, not for lack of room.**
`vocabulary.kinds` (193 bytes) and `vocabulary.relations` (145 bytes) restate material
the payload already carries: the kinds are governed block types the authoring recipes
also describe, and `contradicts` / `supersedes` already appear in `search_guidance`'s
relation-filter example. Cutting both would have landed compact at 55,737, a genuine 263
bytes under the old ceiling, with no gate change at all. They were kept anyway. For a
payload that is a generic client's entire contract, an agent reading the doctrine should
not have to assemble the vocabulary from three other sections to act on it; locality is
worth 338 bytes here. That is the tradeoff — not an impossibility.

So the raise should be described honestly: this change **spent the whole growth budget
the old ceiling expressed and pre-authorised 1,925 bytes more.** It does not restore
equivalent headroom, and a second addition of this size must argue for itself from
scratch. What makes it defensible rather than merely convenient is that the constant's
own comment sets the protocol — "never raise it without deciding the extra bytes earn a
caller's context" — that the decision and its numbers are recorded in that comment, that
58,000 still sits roughly 6 KB below the 64,070-byte regression point the gate was built
to catch, and that every way of staying under 56,000 required cutting the vocabulary's
locality. The cheapest of them, dropping `relations` alone, would have left 70 bytes
clear — a tripwire rather than a budget, where the next unrelated word fails a gate that
is not about it.

**Doctrine can drift from behaviour.** Prose that describes behaviour is prose that can
become false. Mitigated structurally: the outcome vocabulary and the metadata keys are
read from the owning modules rather than restated, and tests assert equality with those
constants. The commitments that are not mechanically derivable — supersede rather than
overwrite, refuted stays active — are stated in the same words as the shipped scaffold
so a reader comparing the two finds one voice.

**A generic client may over-apply the nudge.** An agent told that durable expectations
become predictions could write a prediction for every idle remark about the future. The
clause is scoped to a *durable* expectation about a *future observation*, which is the
same qualifier the entity-capture rule uses to suppress incidental mentions, and the
commitments carry no quota language.

**Version bump ripple.** `contract_version` is compared as an ordered string in one
existing test and asserted exactly in another. Both were checked; the new value sorts
correctly and only the exact assertion moves.
