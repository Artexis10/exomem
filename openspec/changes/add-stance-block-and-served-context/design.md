## Context

See proposal.md for the outcome and scope. The existing catalogue, semantic-unit parser, unit admission, currency projection and capability renderer provide the foundations. Maps remain the default activation mode.

The development comparisons establish no answer-quality gain. A large inline result exceeded the client limit, so that comparison does not establish how an actor uses delivered content. The built workflow needs a check of actor-visible delivery before any behavioral inference.

## Goals / Non-Goals

The increment serves global stance data, centralizes existing recovery moves and records the authorized response metadata. It preserves current map consumers, engagement authority and older-service recovery.

It does not select task-specific guidance, infer task scope, track answer use, change heat attribution or raise compile coverage. The server runs no model and authors no semantic content.

## Decisions

### D1. One response carries the stance block

The block travels beside the existing map and turn slice. A separate optional call would add another retrieval step. A fixed block cap limits its cost; this decision makes no claim that more inline content improves answers.

### D2. Existing admission governs stance pages and units

Only a live compiled page eligible under the existing semantic contract can supply stance lines. Sources, Evidence, pending pages and other ineligible pages do not become stance through a mark. Each unit passes existing admission and terminal egress, including references inside its prose. The block's generation, date boundaries and counts depend only on admitted caller-visible lines.

The page-level boolean marks all its units as global. Marking an existing page therefore requires owner authority for every resulting line. A proposed line or promotion waits for confirmation unless the user already requested that exact action. This control prevents accidental global promotion; its cost is one clarification when the intended scope is uncertain. The agent cannot infer that authorization from a retrieved page.

Stance remains data under current instructions and governance. The mark supplies no authority. Scoped rules belong in ordinary compiled knowledge until the task layer earns its gate. `guidance_origin` remains deferred.

### D3. The stance cache has a bounded lifetime

Whole lines fit under `STANCE_MAX_CHARS`, initially 800, and the remaining counted-prose budget. The cap is a protocol resource bound, not a measured optimum. Count omitted current lines and expired admitted lines. An empty block consumes no stance prose allocation.

Cache the block separately from conversation-dependent packets. Bind it to the caller's released view, admitted stance inputs and the next relevant date boundary. A passed boundary invalidates the cached block even when no page changed. No hidden line affects its public generation or counts. Serving the block is independent of prominence; capture still follows the resolved engagement policy.

### D4. Typed dates reuse the currency projection

Use strict ISO `as_of`, `until` and `check_by`. Superseded or archived units are history. Otherwise a passed `until` makes a unit history without changing its stored lifecycle. Otherwise a passed `check_by` adds `review_due`. Otherwise the unit stays current. `as_of` replaces the prose fallback for the unit's own time.

Use the same-page unit supersession that currency already supports. A replacement on another page must use existing page supersession. This increment adds no cross-page unit resolution. Reconcile the earlier authored-supersession wording before implementation.

### D5. Record composed response metadata, not usage

The existing activation log omits maps refs. Heat cannot fill that gap: it is a lossy page-path ring, and ordinary read/cite events usually lack session attribution. Its citations describe committed writes' sources, not answer citations. Adding transport attribution would also change ranking behavior, so this increment does not do it.

Use one append-only SQLite table in tenant state. Record the final authorized response fields after carriers attach their metadata: map generation and unchanged state, stance and slice refs/states, next-move state and surfaced upkeep identities. Include a usable existing derived session key only when one exists. Never mint a thread or substitute transport identity to fill it.

The owner leaf supports row lookup and summaries of retained rows and ref occurrences. Session totals are unknown where identity is absent. Opens and cites remain visibly unknown with reasons. Do not join empty keys or attribute observations by time proximity. A row proves composed response metadata, not receipt or use, and refs do not preserve historical text. Report a missing row as absent unless retention evidence proves expiry.

Resolve owner authority before disclosing existence. Bind principal and view from the authenticated request, not caller-declared labels. Preserve recorded refs after renames and report current resolution separately. Keep rows out of host logs, including on content-private cells.

Use one synchronous best-effort insert with a bounded timeout, no retries and no propagated failure. Measure its cost rather than promising zero delay. Retention runs outside activation. Keep the existing surfaced-item ledger until equivalent identities, fingerprints, surface coverage and durability are proved; this fallible maps record alone does not retire it.

### D6. One source holds next moves and compatibility text

A central module selects one move from closed response states. Ambiguity wins over review, then empty-slice and existing related-pages recovery apply. Anchor recovery applies only where the existing anchors contract makes `anchor` effective. Maps ambiguity does not acquire that meaning.

Preserve the hook's related-pages instruction through the central source. Render calls with the existing capability renderer and include structured arguments only when complete. Preserve the bootstrap's reduced-surface search carrier. Static surfaces point to `next_action` instead of keeping independent copies of its moves.

A standalone hook can call an older configured service. Generate its bounded fallback from the same move source through the existing artifact pipeline; do not hand-copy it. The fallback preserves existing recovery when `next_action` is absent, and never duplicates a supplied move.

Reserve 240 characters of counted prose for the move before packet caching. Count all new stance/move prose through the existing budget definition. Structure remains outside `max_chars`; refresh `serialized_chars` after final fields are attached. An empty stance block spends none of the separate stance prose allocation.

### D7. Capture authority follows the existing engagement gate

The client model judges whether the rule applies to every task. It proposes the exact line and cites the user's words. An explicit exact-line request already authorizes the write; otherwise an initiated proposal waits for confirmation. Promotion of an existing page covers all its units.

At `light`, write only on request. At `off`, initiate no capture or proposal, but honor an explicit request. Keep replacements on the same stance page or use page supersession. No cue list, classifier or new server review mechanism is added.

### D8. Preserve the existing map field

Keep `activity` and its current value. The deferred task layer does not justify breaking existing readers for a naming cleanup. A later contract change can assess the rename with its actual consumers.

### D9. Reconcile active deltas before implementation

The record changes older no-record wording; typed expiry widens the earlier authored-supersession rule. Update affected active requirements together and preserve all later scenarios. Coordinate removal of bootstrap `miss_rule` with `shrink-bootstrap`, including reduced and frozen surfaces. Do not leave contradictory contracts for whichever change archives later.

## Deferred: task layer

Activities and guidance-role registries, guidance declarations and origin, conversation activities/entities, scope, task guidance and suggestions remain deferred. Their gate is a larger development round on fresh cases where guidance beats both stance-only and client-instruction arms. No new round or task-layer implementation follows from this change.

Compile coverage and the background compiler retain their independent source/key, queue and reach-round dependencies. Attributable usage and retirement of the surfaced-item ledger are later capabilities, not acceptance requirements here.

## C4 and C6 review

C4 is absent from these additions: the mark is boolean and the states are fixed protocol values. C6 semantic word lists and regexes are absent. The client judges meaning; typed date checks validate format. Each added code list or format check must state its closed schema reason.

Existing deadline word cues, semantic category constants and role cue lists remain reported debt. This change does not copy them.

## Proof plan

The highest-level check exercises the first activation and subsequent hook responses on the built bytes. It verifies actor-visible delivery before measuring behavior, and compares each retained row with the final authorized response metadata. It checks unknown usage even when the transcript contains a read.

Use focused parser, admission, cache-boundary and state-move checks where realistic failures differ. Include a visible stance unit linking hidden material, keyless/concurrent activations, record locking and older-server hook recovery. The full suite and independent exact-content review run at delivery.

Real-model checks cover global-scope judgment, exact-line requests, confirmation/decline, each retained move and hostile input. Preserve provider failures at the transport. Keep keys independent, report paired case-level intervals and name any unavailable delivery. No gain is claimed from an interval containing zero.

## Migration and rollout

1. Ship parser and stance changes with central moves, preserving existing consumers.
2. Verify record lookup and unknown usage on the delivered bytes.
3. Roll the stance block back with `EXOMEM_ACTIVATION_STANCE=off` if needed.

Ordinary release authority does not transfer install ownership. Record and move failures remain visible diagnostics while reads continue. The initial caps and latency are unmeasured until acceptance records them.
