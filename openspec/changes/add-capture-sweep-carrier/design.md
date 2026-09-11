## Context

Proactive capture on a hookless client is object-local: the agent writes the one
object it was thinking about and stops. Adjacent durable facts from the same
episode are lost until the user nudges. The question this change answers is
whether the existing carrier machinery can support a bounded
episode-completeness pass after a capture boundary, or whether a new signal
family is needed.

It can, and a new family would be the wrong shape. What follows is the argument,
because the shape is the whole decision.

## Goals / Non-Goals

**Goals**

- Address failure 2 of the dogfood episode: adjacent durable facts from the same
  episode are missed after the first capture.
- Reach the agent at the one deterministic moment the substrate has: the
  response to a durable write.
- Stay inside the existing advisory posture — bounded, validated, absent rather
  than empty, never a key a client branches on for the mutation outcome.

**Non-Goals**

- Failure 1 of the same episode (nothing captured until asked). That is most
  plausibly the balanced contract being served to a hookless client, and it
  belongs to `add-prominence-levels`.
- Any server-side semantic judgement about what is durable. The agent decides.
- Any change to the due-state governor, `batch_scope`, or the first-surfaced
  ledger.

## Decisions

### D1. A carrier, not an audit family

Every family in `audit.ALL_CATEGORIES` is a deterministic predicate over
authored vault state: it has a fingerprint and a resolution condition, which is
what lets `due_state` count it, dedupe it, and stop counting it once the vault
changes. "The conversation held more durable facts than were written" has
neither. There is no vault-state fingerprint for it, because the evidence is not
in the vault; there is no resolution condition, because nothing the user later
writes can prove the episode was completed.

It is also unobservable to a family. The server has no conversation window on a
hookless client: remote HTTP is served `stateless_http=True` in `server.py`,
deliberately, so replicas and restarts do not strand a session — which means
there is not even a per-conversation session id. `due_state.emission_key`
already records this by degrading to one `_PROCESS_SESSION_KEY` for the whole
process. A background analyser has the same problem plus a worse one: it would
be judging conversations it cannot see.

What the server does know, per request and deterministically: who is calling and
from which client, when that caller last wrote durably, what the committed page
names without a page, and what that caller recently wrote. That is exactly the
input set of a write-time advisory, and `structure_suggestion` is the existing
carrier of that class.

### D2. The emission rule is this carrier's own governance, not a change to S6

D7 of `2026-08-29-route-lifecycle-consequences-without-nudges` rejects a
session-level debounce, in as many words: "A session-level debounce would be a
change to the governance itself, not to this carrier." That ruling is about the
DUE-STATE block, whose governance is the change-only digest rule — successive
writes that genuinely change the totals are supposed to say so each time, and
debouncing them would suppress true changes.

This is a different advisory with a different subject. Its subject is not vault
state at all; it is the episode boundary, and a quiet interval is the only
deterministic proxy for one. Silencing the second write inside the interval is
not suppression of a true change, it is the whole content of the signal: "you
have just entered capture mode" is only true once.

So the S6 governor is untouched, `due_state.batch_scope` is untouched, the
first-surfaced ledger is untouched, and `capture_sweep` carries its own ledger,
its own constant, and its own boundary predicate. The two carriers share the
seam and nothing else.

### D3. The ledger keys on the caller, and the tiers have costs

The key is `(scope, client, vault)`, where `scope` is
`command_surface.mcp_retry_scope()` and `client` is
`mcp_caller_identity()["client_name"]`. `mcp_retry_scope()` returns one of four
things, and each is a different tier with a different cost:

- `principal:<hash>` — a verified OAuth subject. Stable across token refresh.
  This is the intended key and it has no cost.
- `bearer:<hash>` — a hash of the raw credential. Correct while the credential
  lasts; a token refresh mints a new key, so the caller is re-armed once. One
  extra advisory per refresh is an acceptable price and is stated here rather
  than hidden.
- `session:<id>` inside an HTTP call — unstable under `stateless_http=True`. Not
  usable, and the answer is to EMIT NOTHING rather than to collapse every caller
  into a single bucket, which would let one principal's write silence another's.
- `None` inside an HTTP call — same answer, same reason.

Outside any MCP call — stdio, the CLI — identity is `None` by design, and the
process lifetime IS the conversation. The key is then a process-lifetime key,
exactly as `due_state._PROCESS_SESSION_KEY` is and for the same reason.

The vault is in the key because one process can serve more than one.

The ledger is in memory and is not persisted, by the rationale `due_state`
records for `_EMISSION`: "persisting it would make a server restart change what
an agent is told." A restart re-arms every caller once. The cost of that is one
extra advisory; the cost of persisting it is a durable file that decides what an
agent is told, which is a worse thing to own.

### D4. The ledger records writes, not deliveries

`due_state` deliberately splits production from delivery: it records
`mark_emitted` at the terminal, because recording at production burnt the
session's one emission on responses that never carried the block.

This carrier records at production, and the difference is not an oversight. The
due-state ledger answers "has this caller been TOLD this?" — so it must record
what was told. This ledger answers "has this caller WRITTEN recently?" — a fact
about the caller, not about what reached them. A write that happened is a write
that happened whether or not its response carried an advisory, so recording it
at the seam is the correct semantics, and an advisory lost to a `legacy` detail
costs the caller one advisory rather than corrupting the boundary.

### D5. One block per invocation, including a batch

A single write is one invocation and gets at most one block, because
`project_terminal` runs once. A multi-write product command is the case that
needs a rule, and it reuses the one the substrate already has: inside
`due_state.batch_scope` the seam neither produces nor records, and the batch
carrier in `commands` — the mirror of `_carrying_due_state`, gated on
`writer_lease.active_mutation_committed()` — evaluates the boundary once after
the scope exits, records once, and attaches at most one block. A twelve-write
curation pass is one episode boundary, not twelve, and it consumes one ledger
entry rather than twelve.

The boundary decision and the ledger write happen under ONE lock acquisition
(`capture_sweep.check_and_record`). Asking `boundary` and then calling
`record_write` is two acquisitions with a gap, and two concurrent writes on one
key would both read a quiet interval and both emit — the one duplicate this
governance exists to prevent. The two primitives stay public because each is
separately meaningful and `boundary` records nothing, which is what makes it safe
to ask twice in a test; no production path calls them in sequence.

### D6. Hints are best-effort and cost one digest-cached file read

`unpaged_mentions` comes from the body wikilinks the preflight already parsed,
resolved against the corpus context the preflight already built
(`SemanticCorpusContext` carries `pages` and the resolver sets), minus anything
the entity registry answers to.

The registry filter is the one cost, and it is stated rather than rounded to
zero: `entity_recurrence.registry_index` is fed page states already in memory,
but it needs an `EntityTypeRegistry`, and `entity_types.load_entity_types` opens
the vault's entity-type extension file. That is one small file read per call,
served from a digest cache after the first, and it is the same single file
`audit`'s own recurrence sweep opens for the same reason — its docstring calls
it "exactly one file". So: no writer lease, no vault-wide read, one digest-cached
file.

Nothing here is promised beyond that: a first-mention plain-text name with no
wikilink is not detected, and the block is still useful without it. When the
corpus context is absent the hint list is simply empty.

The `consider` list on the wire is EXAMPLES and says so, and the prose carriers
say "for example" and never "only". This is not stylistic. The contract-design
finding is that agents read an enumeration inside a capture contract as a closed
boundary, which would make this carrier narrow capture rather than widen it.

### D7. The hosted skill picks the clause up at the next candidate mint

The clause belongs on the hosted agent surface and is deliberately NOT written
there by this change. `plugins/hosted/skills/exomem/SKILL.md` is not an editable
file: its bytes feed the release-locked v1 hosted identity
(`tests/fixtures/hosted/v1-release-identities.json`,
`plugins/hosted/generated/compatibility.json`, and the v1-v4 immutability
manifest), so an edit turns
`test_v1_hosted_release_identity_fixture_remains_immutable` and
`test_claude_archive_is_deterministic_and_locked` red, and the regeneration that
would settle them is release-owned rather than lane-owned.
`hosted_plugins.SELF_CONTAINED_CANDIDATES` records the same constraint in as
many words: it is why the four #1085 doctrines were written into a v5 candidate
instead of the shared files.

So the hosted carrier is a named follow-up rather than a gap: the clause reaches
the hosted surface at the next hosted candidate mint, carried by whoever owns
that candidate, alongside whatever fresh evidence the mint requires. Until then
a hosted client still receives the clause through the bootstrap payload, which
is the carrier every hookless client reads first — the skill copy is the
redundant second statement, not the only one.

### D8. Contract bytes

The prominence `capture` strings at `balanced` and `maximal` gain one clause and
lose nothing; `light` and `off` are untouched. One
`capture_sweep_handling` entry joins `authoring_contract.post_write` and
`commands._SESSION_POST_WRITE_KEYS`. The shipped scaffold engagement reference
carries the same clause, and the local plugin skill copies are regenerated from
it. The copy-paste blocks in `docs/prominence.md` do not; see below.

Every one of those texts is a SUPERSET of what it held at HEAD. A byte-budget
compression once silently dropped the Planning/Records transition rule from the
compact projection and seven reviews missed it; `tests/test_prominence.py`
carries the base-sentence pins that regression produced, and this change adds
its own superset check rather than trusting a diff read.

The compact bootstrap payload is measured at `balanced` and `maximal` before and
after and reported with the delivery. The ceiling in
`tests/test_bootstrap_compact_budget.py` is NOT raised.

The two copy-paste blocks in `docs/prominence.md` do NOT carry the clause, and
that is a measurement rather than an omission.
`tests/test_personal_baseline_contract.py` caps every copyable instruction block
at 1,500 bytes, because that is the size of the custom-instruction field they are
pasted into. The prominent blocks measure 1,484 and 1,495 bytes — 16 and 5 bytes
of headroom. No wording of this clause fits, and the only way to make one fit is
to shorten a capture class that is already there.

That was declined on the merits. A byte-budget compression once silently dropped
the Planning/Records transition rule from the compact projection and seven reviews
missed it; `tests/test_prominence.py` still carries the base-sentence pins that
regression produced. Spending an existing rule to buy a new one is the same move.

The doctrine is not lost to those clients. Both blocks open by telling the client
to call `bootstrap(profile="compact")` and follow it, and the compact payload
carries the full clause plus `capture_sweep_handling`. The paste block is the
redundant second statement; the payload is the primary carrier. Adding the clause
there is a follow-up for whoever next trims those blocks, and
`tests/test_capture_sweep.py` pins the arithmetic so the next reader does not
re-derive it and so it turns red the day the room appears.

### D9. No tool-surface movement

The digest hashes `outputSchema` (`tool_surface.py`) and every leaf returns a
bare `dict`, so a new response key does not move it. No tool description and no
input schema changes. The tool-surface suites are part of this change's gate so
the claim is checked rather than asserted.

### D10. Acceptance without a bench amendment

The reference journey is `tests/test_capture_sweep_journey.py`: seed a client
entity page and a prior dated note, recall, write a meeting page (the advisory
fires and `unpaged_mentions` names the booking system), write the quirk page (no
advisory), then replay both writes and prove idempotence.

That is a deterministic composition test and it does NOT replace the real
measurement. The real acceptance is a replay family in the f27 shape — real
agent, maximal, no "save this" language — and registering it as f28 needs a
dated §7 sequence-4 amendment with a founder receipt, which the scenario loader
enforces by refusing unregistered families. It is named here as the follow-up
change `amend-no-nudge-bench-families-seq4`, and no comparative no-nudge result
is claimed until it exists.

## Risks / Trade-offs

- **A caller on the `session`/`none` tier is silent.** Accepted: a wrong bucket
  is worse than silence, and the tiers observed in the live cell's call ledger
  decide how much of the fleet this affects.
- **A bearer-token caller is re-armed once per refresh.** Accepted and stated.
- **The advisory arrives once per quiet interval per caller, whether or not the
  episode held anything else.** That is the cost of having no conversation
  window; the rule tells the agent to stay silent when nothing qualifies, and
  the agent is the decider.
- **`unpaged_mentions` misses a plain-text first mention.** Accepted: the block
  is useful without it, and detecting it would need exactly the semantic
  judgement this design refuses to put on the server.

## Open Questions

- Which hosted candidate mint carries D7's clause, and when.
- Which scope tier claude.ai and ChatGPT actually land on against the live cell.
  The tier rule above is correct for all four outcomes, so this decides coverage
  rather than correctness.
