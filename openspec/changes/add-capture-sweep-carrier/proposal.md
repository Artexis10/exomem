## Why

Proactive capture on a hookless client is object-local. The agent writes the one
object it was thinking about, the turn ends, and the adjacent durable facts from
the same episode — an operational quirk of a supplier, an outcome, a facet of a
recurring entity — are lost until the user notices and asks. A 2026-09-11 dogfood
episode recorded exactly that shape twice in one conversation, under a maximal
preference.

Nothing in the substrate can notice this today. The server has no conversation
window on a hookless client: remote HTTP is served `stateless_http=True`, so there
is not even a per-conversation session id, and `due_state.emission_key` already
degrades to one process-lifetime key. The only actor that can see the episode is
the agent, and the only deterministic moment at which the substrate can address
the agent about it is the response to a durable write.

## What Changes

- Add `capture_sweep`, a write-time advisory carrier of the same class as
  `structure_suggestion`: produced at the post-commit seam, validated and
  attached by the mutation terminal, governed by its own small emission rule.
- Govern it with an in-memory quiet-interval ledger keyed on the caller, not on
  vault state: the first durable write after a quiet interval carries the
  advisory and the writes that follow it do not.
- Carry a bounded, best-effort `unpaged_mentions` hint and the caller's recent
  write references, so the agent can dedupe rather than re-write.
- Teach the same bounded episode-completeness pass in the prominence capture
  contract at `balanced` and `maximal`, in the bootstrap post-write guidance,
  and in the shipped scaffold engagement reference. The copyable
  custom-instruction blocks have 16 and 5 bytes of headroom under their
  1,500-byte cap, so they are left unchanged rather than trimmed; those clients
  receive the doctrine through the compact bootstrap payload those same blocks
  tell them to fetch.
- Add no audit family, no background analyser, no env knob, no tool-schema
  change, and no hook change. The agent stays the sole semantic decider.

## Capabilities

### Modified Capabilities

- `command-surface`: compiled and structured write responses may carry one
  bounded advisory `capture_sweep` block under its own emission rule.
- `agent-bootstrap-contract`: bootstrap teaches how to read the block and the
  prominence capture contract names the bounded episode-completeness pass.
- `portable-agent-contract`: the shipped scaffold carries the same pass; the
  capped copyable blocks are left to the bootstrap payload they already point at.
- `hosted-agent-surface`: the hosted agent skill carries the same pass at the
  next hosted candidate mint. See `design.md` D7 — the shared hosted skill file
  is release-locked into the v1 hosted identity, so this change does not edit
  it and task 4.6 stays unchecked for the release owner.

## Impact

Affected areas: a new `src/exomem/capture_sweep.py`, the two page-write
post-commit seams in `semantic_writes`, the structured-write carrier in
`records`, the advisory projection in `mutation_terminal`, the batch carriers and
post-write contract in `commands`, the two prominent `prominence` capture
strings, the shipped scaffold engagement reference, and the local plugin skill
copies regenerated from it. `docs/prominence.md` is NOT changed: its copy-paste
blocks have 16 and 5 bytes of headroom under their 1,500-byte cap. No tool
description, input schema, tool-surface digest, hook, or generated hosted
artifact changes.

The deterministic journey test added here is not a real-agent measurement. The
replay family that would measure it (f28, the f27 shape) needs a dated §7
sequence-4 amendment with a founder receipt, and is named as the follow-up change
`amend-no-nudge-bench-families-seq4`.
