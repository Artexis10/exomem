# Design — route lifecycle consequences without nudges

## Context

Two facts fix the shape. First, the hard split in the tree: only the CLI hooks see the
conversation (`_hooks/exomem_capture_nudge.py`, `exomem_retrieve_nudge.py`); only
bootstrap, the response carriers (`mutation_terminal.project_terminal`, recall,
bootstrap) and the attention surface reach hookless clients; no seam does both. A
universal deterministic layer therefore cannot detect "the user said it was done" — it
can only detect "a record exists and the plan item is still open". Second, the
constitution: the server measures, the brain reasons. The runtime never performs a
Planning transition; it reports one that appears owed, in the same channel S1 built
and S6 governed.

So the change is one agent-contract widening (the decider learns two more capture
classes) plus one sensor family over authored state (the verifier that catches a miss
from any client), wired into the existing carriers rather than a new surface.

## D1 — Two capture classes, one pairing rule, never performed by the runtime

The capture axis in `prominence.py` currently names three classes. It gains, at
`balanced` and `maximal`:

- *stated intent or commitment* — the user says what they will do, commits to a
  batch or workstream, sequences work ("the next one", "the others next time"), or
  re-prioritises → Planning (`plan_memory`: add into the inbox state, or `triage` /
  `update` an existing item).
- *observed outcome or event* — the conversation reports that something happened,
  was produced, measured, delivered, approved, published or failed → Records
  (`record_memory` append into the one compatible collection).

Pairing rule: an observed outcome that lands on an open committed Planning item is one
landing with two consequences — the Records append first (canonical observation),
then the Planning transition (status, and lifecycle where the collection's convention
archives completed work) — performed together and reported once. Tentative claims
stay out of Records: uncertainty is represented by absence plus whatever note field
the manifest offers, never by a fabricated event. Elapsed time is never an outcome.

`light` keeps "capture only when asked"; `off` is untouched. The existing Records
engagement policy (exactly one compatible collection and a sufficiently identified
observation → write and report; competing collections → one question; no collection
→ propose, never silently create) is the write gate for both classes.

Why a contract change and not a runtime one: the evidence for these classes exists
only in the conversation, which is the attribution test's definition of an
agent-contract gap. The hook is the only enforcement that sees the conversation, so
it learns the same classes; on hookless clients the `maximal` default and bootstrap
carry it.

**Rejected:** a runtime "transition advisory" that reads the turn. Nothing
hookless can read it.

## D2 — Domain language, store named only as citation

The report line for a lifecycle consequence states what is now true in the user's
vocabulary ("Muscle is done and logged; Sleep and Standing stay queued") and cites
the page or collection the way recall cites pages. The words Planning, Records,
collection, schema and natural key are never required of the user and never lead the
report. This is the same rule the simple front door already applies to Sources /
Notes / Entities, extended to the two profiles.

## D3 — Identity from the declared natural key

`append_record` mints `uuid4` when `item_key` is omitted. Every manifest already
declares a `natural_key` (required, 1–16 declared fields), and the read path already
knows how to serialise it (`natural_key_serialization` → `inferred_item_key`, a
`uuid5` in the collection's namespace). The write path now uses the same function:

- explicit `item_key` still wins;
- otherwise, when every natural-key field is present in the validated values, the
  key is `inferred_item_key(collection_id, natural_key_serialization(...))`;
- otherwise `uuid4`, as today.

The derived key is stamped explicitly into the item like any other, so the existing
identity requirement ("new agent-authored items receive an explicit UUID item key")
holds and a later correction of a natural-key field does not move the identity —
the legacy-inferred scenario stays what it is.

Consequences fall out of the existing replay rules: an identical re-stated append
returns `replayed`; the same natural key with different content refuses as
`RECORD_ID_CONFLICT` and the agent updates instead. One new refusal closes the hole
those rules cannot see: when the derived key differs from an existing item's key but
the serialised natural key equals that item's, the append refuses with
`RECORD_NATURAL_KEY_CONFLICT` naming the existing item(s) — so a pre-change
`uuid4`-keyed item cannot be shadowed by a new derived-key twin. The check runs over
the adapter snapshot the append already loads; no extra read.

Planning inherits all of it through `plan_id` → `item_key`; a Planning collection
keyed on `[title]` now refuses a second "Sleep" rather than filing a duplicate.

**Rejected:** fuzzy or case-insensitive matching on append. Identity is the
manifest author's declaration; the product's job is to honour it exactly.

## D4 — Planning inventory

`plan_memory(action="inspect")` with `collection` omitted returns the Planning
inventory: every manifest whose profile is `planning`, in the same bounded shape the
Records inventory uses (`record_governance` inventory filtered on profile), with the
same disclosure filtering. With a collection it behaves exactly as today. Bootstrap
teaches the form; the `plan` simple action joins `_SIMPLE_ACTIONS` so the compact
payload's simple-action table matches `SKILL.md`. The generated `plan_memory` schema
already lists `collection` as optional and the action enum is unchanged, so this
moves no schema bytes; the description moves the pin, and it moves anyway (D9).

**Rejected:** a new `match` action that resolves an observation to an item.
`query` with `filters` on `title` / the natural-key fields plus `lifecycle` and
`status` already answers it exactly; the agent is taught that form, and the runtime
match lives in D6 where it is deterministic.

## D5 — The binding: a `join` on the Records→Planning link

`links.plans[]` on a Records manifest holds `{reference, query}` today, where
`reference` is an opaque `exomem://memory|vault|source/...` and `query` a bounded
Records descriptor. It gains an optional `join`: a mapping of one to four record
fields to plan fields. Record-side names must be declared schema fields; plan-side
names are bounded non-empty text (`title` or a natural-key field of the target, which
Records cannot check because Records does not resolve the target). The Planning
collection is named by the memory reference of its manifest page —
`exomem://memory/<manifest-uuid>` — which `_opaque_plan_reference` already accepts;
no new URI scheme.

Records validates, round-trips and governance-projects the `join` exactly as it does
`query`, and still performs no Planning lookup in any Records operation. The only
resolver is the attention family (D6). This keeps the records spec's boundary intact
and puts the declaration on the observation side, where "these events are about
those deliverables, matched on title" reads naturally.

**Rejected:** binding on the Planning side through `progress_evidence`. That
descriptor is per item and names a saved view for planned-versus-recorded review; it
answers "how is this item progressing", not "which events are about which items".
Both stay; they are different questions.

## D6 — The `unreflected_outcomes` family

Definition. For each Records manifest with a `join`-bearing Planning link whose
reference resolves to a Planning manifest: for each **open** Planning item — lifecycle
`active` and status not in `{completed, cancelled}` — the set of records whose join
fields equal the item's is computed; a non-empty set is one finding.

- Finding: `category="unreflected_outcomes"`, the plan item's reference, the Records
  collection, the joined record references (first 8 plus the total), the binding
  that produced it. Severity follows `prediction_window` (a review candidate, not a
  defect). `signal_version` derives from authored state only.
- Fingerprint: the item reference and the sorted joined record keys. A new joined
  record changes the fingerprint, so a dismissed item resurfaces when a new event
  lands on it (the existing material-change rule); the dismissal record stands.
- Resolution: the item leaves the open state, the binding is removed, or the joined
  records are gone. Never by time, never by the runtime editing either side.
- Disclosure: the same release-plane filtering as every other category; a withheld
  record or item contributes nothing and the served view equals the vault with it
  absent.

Placement. The family joins `audit.ALL_CATEGORIES`, the **default** attention union,
`due_state.PROJECTION_CATEGORIES` and `DELTA_CATEGORIES`, and is therefore
automatically a registered family for S6 dispositions. Default rather than opt-in:
the two opt-in queues are held back by a grandfathered population
(`unfinished_experiments`) or a threshold the product chose (`question_aging`);
this family fires only on a binding a person declared in a manifest, which is the
`prediction_window` precedent, and a vault without bindings sees nothing.

Write-time delta. A structured write settles this category for what it touched, from
the committed write itself plus at most one counterpart snapshot.

*Record side* (append / update). The written record is the caller's own committed
write, so its join value is already in hand; the only unknown is which open plan
items that value lands on. The delta reads exactly one thing — the bound Planning
snapshot, UNFILTERED — and edits only the entries for the items whose join key
equals the record's new value or, on an update, its previous one: merge the
record's `(path, key)` into that entry's joined set, remove it from entries keyed by
the old value, drop an entry whose set empties. It never re-reads the Records
collection to recount, and it never asks the release plane about anything.

*Plan side* (add / update / triage). Reads nothing in the common case: an item that
left the open state has its entry popped, and an item still open whose join-side
values did not move is recomposed in place from the pairs already stored. Only an
`add`, or an update that moves a join-side value, reads the bound Records
snapshot(s) — unfiltered — to rebuild that one item's entry.

*Binding discovery never walks the vault.* The record side asks the manifest it was
handed (`links.plans[].join`); the plan side consults a `bindings` index persisted in
the projection, rebuilt by `reconcile` and extended by any record-side delta that
resolves a binding the index did not have. An unbound write therefore pays no
discovery at all.

*Disclosure is decided once, at serve.* The projection is server-internal truth; the
served view drops joined records the reading audience may not see and recomposes the
fingerprint from the survivors. Filtering at write time as well cost a policy load
per item file — 55% of a 33 s write — to reach an answer the serve boundary reaches
anyway, and it made the stored projection depend on whoever wrote last. The audit
pass keeps its filter, because that one IS a read surface; it now loads the release
policy once per pass rather than once per path.

*Page writes are a different shape and settle a different set.* `unreflected_outcomes`
is a property of a bound collection PAIR, so an ordinary page write can neither
produce it nor prove its absence: `PAGE_DELTA_CATEGORIES` excludes it and
`STRUCTURED_DELTA_CATEGORIES` is the remainder, derived from the two rather than
restated.

The read is bounded by the declared bindings and the bound collections, not by the
vault — the S1 argument that write latency must not scale with the corpus holds, and
the cost is measured (tasks 4.7). `reconcile` remains the healer and the only full
recomputation.

Relation to plan-progress. `review_memory(mode="plan-progress")` evaluates bound
views per committed item on demand and presents divergence; it stays out of the
inbox by its own delta. `unreflected_outcomes` is the always-on backstop: cheaper,
coarser, deterministic, and it never scores or mutates either.

**Rejected:** inferring "done" from event values. The family reports that events
touched an open item; whether `recorded` closes it, `rerecord-needed` re-queues it or
`published` is merely later history is the decider's call.

## D7 — Structured mutations as carriers

`record_memory` (`append`, `update`) and `plan_memory` (`add`, `update`, `triage`)
responses carry the bounded advisory due-state block exactly as the page-write
terminal does: `due_state.apply_write_delta` for the category the write can settle,
then `served` → `mark_emitted` under the S6 emission governance (`batch_scope`,
dispositions, first-surfaced ledger, fail-closed to silence on an unreadable store).
The leaves DO pass through `project_terminal`: the structured writer attaches the
produced block to its receipt as a leaf `due_state` key, and the terminal lifts that
key exactly as it projects a page write — one place decides emission, one place
strips the internal routing hint, and a test proves one block per response and one
per batch. The advisory is produced AFTER the mutation guard releases (M1): it is
derived state that changes nothing another writer could observe, and holding the
vault's single writer lease across an audit-shaped read made a third of the critical
section other writers' queueing time.

Successive single `record_memory` calls each carry a block, and that is the intended
behaviour rather than a governance gap: the S6 governor suppresses an identical
total, and each of those writes genuinely changes the total. A session-level
debounce would be a change to the governance itself, not to this carrier.

With this, the response to the append that opens a gap is the response that reports
it — inside the same turn, on every client.

## D8 — Contract bytes

`intent_boundary` gains the two classes and the pairing rule; `capture_examples`
gains one paired example; `_SIMPLE_ACTIONS` gains `plan`. The compact payload is
measured before and after; the ceiling in `tests/test_bootstrap_compact_budget.py`
(60,400, headroom 392 B) may rise by the minimum needed and not past 61,400. A
compact-bootstrap trim is still its own queued change; this delivery does not do it.

## D9 — One pin move, two causes

Completing `add-planned-vs-recorded-review` task 7.1 rewrites the `review_memory`
`mode` docstring, which moves the packaged tool-surface digest; the `plan_memory`
description (D4) moves it in the same regeneration. Regenerate as
`docs/remote-quickstart.md` prescribes: `scripts/dump-tool-schemas.py`, the packaged
contract, the v1 release-identities fixture, the hosted generated locks and directory
packets, the ChatGPT plugin contract's pending digest with `refresh_required: true`.
The connector refresh is a post-release step. The archive of that change happens in
this delivery (`openspec archive add-planned-vs-recorded-review --yes`), with its
`MODIFIED` block for "Neutral observed-state query views" refreshed against the
current canonical text so later additions survive.

## D10 — Acceptance without a bench amendment

The reference journey lives in `tests/test_lifecycle_routing_journey.py` and reuses
the bench seeding helpers (`seed_journey_vault`, the envelope driver) but is not a
registered family, so the ratified pre-registration does not change. The
`VaultProjector` neutral snapshot gains a `collections` section — profile, manifest
reference, and per item its key, lifecycle/status where the profile has them and the
natural-key values — additive and versioned, no assertion or predicate touched. The
slice-2 harness (multi-turn agent replay with the magic-word filter) registers the
family through the §7 sequence-3 amendment and diffs these snapshots; this delivery
gives it a fixture, a projector and a deterministic expectation to diff against.

Fixture vocabulary is generic: a "batch production" workstream of "deliverables",
events `produced | approved | delivered | published | rejected | redo-needed`. No
personal, product or vault-structure label (the scaffold no-leak rule extends to the
fixture by policy).
