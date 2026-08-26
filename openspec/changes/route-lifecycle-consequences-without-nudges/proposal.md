# Route lifecycle consequences without nudges

## Why

A real creative batch-production session on 2026-08-22 (KB:
`Notes/Research/Exomem/tu-reel-dogfood-planning-and-records-should-disappear-behind-ordinary-work`)
showed Planning and Records working well once they existed — and every lifecycle
consequence still waiting for the user to say "use Planning", "write the record",
"close that one". The ordinary language already carried the information: "let's do
the next one" (intent: the next queued deliverable is active), "this one turned out
really well" (outcome: a usable take was produced), "three done" (three work items
completed), "I'll do the others next time" (the rest stay queued), "Kim posted it"
(a publication event). An expert user would have turned each of those into a
structured-collection mutation without being asked. The agent did not, and nothing in
the substrate noticed.

The no-nudge programme's attribution test says where a required nudge belongs: it is
a **runtime gap** when the evidence already existed in durable state, an
**agent-contract gap** when the evidence existed only in the conversation, and not a
nudge at all when the utterance introduced new intent. This session has both of the
first two, verified against the current tree:

1. **Agent-contract gap.** The capture predicate is closed over three classes —
   a durable conclusion, a recurring entity, a carried-out method whose outcome is
   reported (`src/exomem/prominence.py:107-161`, the stepping-stone block of both
   `SKILL.md` copies, bootstrap `intent_boundary` at `commands.py:438-449`). Stated
   intent and observed outcomes are not capture classes. Planning is taught as
   request-driven ("never infer it from prose", `SKILL.md:317,359`); Records has one
   proactive clause (`SKILL.md:326-331`) that the implicit-proactive block
   (`SKILL.md:606-609`, which lists only `ask_memory`, `capture_source`, `remember`)
   does not mention. The Stop-hook write detector
   (`_hooks/exomem_capture_nudge.py:44-51`) does not count `record_memory`,
   `plan_memory` or `observe_memory`, so a turn that did the right thing is nudged
   anyway, and its reminder names no lifecycle class. This is the same defect class
   the cooking failure exposed for methods and #607 (`cover-executed-method-outcomes`)
   fixed for that one class.

2. **Runtime gap.** Given the expert end-state the session eventually produced — a
   Planning collection of queued deliverables keyed by title, and a Records collection
   of production events keyed by `(occurred_on, title, event_type)` — the substrate
   cannot see that a recorded event touched an open plan item. No audit category,
   attention family or due-state projection knows structured collections
   (`audit.py` has zero references); `record_memory` and `plan_memory` responses
   carry neither the structure advisory nor the due-state block (they never pass the
   mutation terminal); the Records→Planning manifest link accepts only an opaque
   reference plus a query descriptor and cannot express a join; `append_record`
   ignores the manifest's declared natural key and mints `uuid4` when the caller omits
   a key (`records.py:153`), so a re-stated event or a re-added work item duplicates
   instead of replaying; `plan_memory(action="inspect")` requires a collection
   (`plan_memory.py:62`), so a fresh session has no Planning inventory the way Records
   has one; and planned-versus-recorded review shipped (`plan_progress.py`, 38 tests)
   but its task 7.1 is open, so MCP clients cannot discover `mode="plan-progress"`.

3. **Project emergence** (a workstream that deserves its own Planning surface before
   any note scope diverges) is covered by nothing — structural promotion is a
   tag-vocabulary shape test over pages (`structure_promotion.py`) and Planning items
   are excluded from it by spec. That is a separate signal family and a separate
   change; this proposal names it and stops.

The note proposes "a post-turn advisory that detects conversation-implied
transitions". That mechanism is not universally buildable: only the CLI hooks see the
conversation, and only bootstrap, the response carriers and the attention surface reach
hookless clients (hosted, claude.ai, ChatGPT). No seam is both. So the deterministic
layer must key on **authored state** — a record landed, a plan item is still open —
and the conversation-side inference stays where it can exist: the agent contract on
every client, plus the hook where there is one. That is the programme's
sensors → verifiers → one decider shape applied to lifecycle: the agent decides, the
substrate measures and carries, nothing in the runtime performs a transition.

## What Changes

**Layer 1 — the agent contract routes lifecycle consequences from ordinary language.**

- The capture predicate gains two classes at `balanced` and `maximal`: *stated intent
  or commitment* → Planning, *observed outcome or event* → Records, with the pairing
  rule that an observed outcome which lands on an open committed Planning item is one
  landing with two consequences (the Records append, then the Planning transition),
  performed together and reported once. Tentative claims ("probably not posted") are
  never written as events. The agent reports in the user's domain language and names
  the store only as a citation.
- Both `SKILL.md` copies, `prominence.py`, the bootstrap `intent_boundary` /
  `capture_examples` / simple-action teaching, the `continue` and `capture` workflow
  skills and the hosted skill renders carry the same rule; the Stop hook counts
  structured-collection writes as KB writes and its reminder names the lifecycle
  classes.

**Layer 2 — the substrate can see a missed consequence and carry it.**

- Appends derive the item key from the manifest's declared natural key when the
  caller omits one, so a re-stated event replays and a re-added work item refuses as
  a conflict instead of duplicating. Planning inherits this through the shared
  mechanics.
- `plan_memory(action="inspect")` without a collection returns the Planning
  inventory, mirroring the Records inventory.
- A Records manifest's Planning link may declare a bounded `join` (record field →
  plan field). Records still validates and round-trips it without resolving anything.
- A new deterministic attention family, `unreflected_outcomes`, reads those
  bindings: an open Planning item (active lifecycle, status not completed or
  cancelled) that at least one record joins to is a review candidate, fingerprinted on
  the item and the set of joined records, resolved when the item leaves the open
  state, and dismissable through the existing triage and disposition surfaces. It
  joins the default attention union and the due-state projection; a structured write
  applies a bounded per-pair delta and `reconcile` heals.
- `record_memory` and `plan_memory` mutations become due-state carriers under the S1
  carrier contract and S6 emission governance, so the response to the record append
  that opens a gap is the response that reports it.

**Acceptance — a deterministic reference journey.** A test-level journey seeds a
generic batch-production fixture (one Planning collection of queued deliverables, one
Records collection of production events joined on title), replays the expert
end-state minus the Planning transitions, and asserts: the family fires on exactly the
touched items and on none of the twins; the carrier reports it on the record append's
own response and once per batch; the transitions clear it by state change, not by
dismissal; the dismissal survives passes; and the `VaultProjector` neutral snapshot
now exposes both collections so the slice-2 comparator can diff durable state. The
journey is not registered as a bench family — that registration is the §7 sequence-3
amendment owned by the agent-track replay harness change.

**Closure.** Task 7.1 of `add-planned-vs-recorded-review` (docstring and tool-surface
regeneration) is completed here, that change is archived in this delivery, and the
pinned tool surface moves once for both.

## Capabilities

### Modified Capabilities

- `agent-bootstrap-contract` — lifecycle capture classes, domain-language reporting,
  Planning inventory and `plan` simple action, the hook's write detector.
- `structured-collections` — natural-key-derived identity on append.
- `records` — a `join` mapping on the opaque Planning link.
- `planning` — Planning inventory before a selector is known.
- `attention-queue` — the `unreflected_outcomes` family and its due-state placement.
- `command-surface` — `inspect` without a collection; structured mutations as
  due-state carriers; plan-progress discoverable in the tool surface.
- `epistemic-state-bench` — the projector exposes structured collections; no family,
  assertion, predicate or gate changes.

## Impact

- Code: `src/exomem/prominence.py`, `commands.py` (bootstrap teaching, simple
  actions, carriers on the two structured leaves, `review_memory` docstring),
  `records.py` / `structured_collections.py` (natural-key identity, conflict),
  `record_governance.py` (join validation and projection), `plan_memory.py` /
  `planning.py` (inventory), `audit.py` + `attention.py` + `due_state.py` +
  `review_state.py` (family registration, projection, delta),
  `_scaffold/_Schema/SKILL.md` and the plugin copy, `workflow-skills/{continue,capture}`,
  `_hooks/exomem_capture_nudge.py`, hosted skill renders and generated locks,
  `benchmarks/epistemic/projectors/exomem_vault.py`, `docs/epistemic-inbox.md`,
  `docs/records.md` or its equivalent.
- **Tool-surface pin moves once** (descriptions only: `review_memory` gains
  `plan-progress`, `plan_memory` describes the inventory form). No parameter is added
  or removed: the `plan_memory` schema already lists `collection` as optional and the
  action enum is unchanged. Regenerated as `docs/remote-quickstart.md` prescribes.
- Compact bootstrap bytes: measured; the ceiling in
  `tests/test_bootstrap_compact_budget.py` may rise by the minimum needed and not
  past 61,400.
- New error code `RECORD_NATURAL_KEY_CONFLICT`; new attention category
  `unreflected_outcomes` (default union, projection, delta, disposition-registrable).
- **Compatibility break.** Natural-key identity is enforced on every write, not
  only on append: an update that moves an item onto another item's declared
  natural key now refuses with `RECORD_NATURAL_KEY_CONFLICT`, and Planning
  updates inherit it (triage cannot reach `title`, the natural key these
  collections declare, so ordinary lifecycle work is unaffected; a collection
  keyed on a field triage can reach would refuse the same way). Existing
  collections that already hold two items under the same natural key -- reachable
  before the append check existed -- refuse every further append for that key
  until one twin is corrected. Recovery: update one of the
  named items to a distinct natural key, or delete/archive one, then retry; an
  update that leaves the key unchanged is never refused, so the corrective edit
  itself always goes through. Documented in `docs/records.md`.
- Out of scope, deliberately: the agent-track replay harness (north-star comparator;
  own change with the §7 sequence-3 amendment); project emergence from Planning state
  (own change); any runtime-performed Planning transition (never — the runtime
  measures and carries, the agent decides); exposing `plan_memory` on
  `hosted-alpha-agent-v2` (surface profile decision, unchanged).
