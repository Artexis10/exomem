## Why

The dreamer models the vault from structure alone: links, shared sources and graph shape. It cannot tell that a later note refines an earlier one, that two notes contradict each other, or that a recap already covers a page. Those are the relations an epistemic engine exists to surface, and only a model can measure them. Today the one admitted model (the frozen NLI stance verifier) runs inside an audit request, labels only a queue's surfaced pairs, and leaves no durable record: every sweep re-runs it, and nothing downstream can reason over what it measured.

The owner's rulings of 2026-09-28 settle the direction. The dreamer stays Exomem's deterministic epistemic modelling engine, and it never becomes a generative second brain. Models become recorded **instruments**: each answers one closed question, with a closed label set plus abstain, into a durable readings ledger. The dreamer models deterministically over those readings and the graph. Delivery stays pull-first, and the agent remains the sole decider.

## What Changes

- Add a **question registry**. Each entry is closed: a versioned template, a versioned label map over a closed label set plus `abstain`, and a unit scope. Build order:
  1. `pair.relation` over claim units: `contradicts`, `refines`, `restates`, `neutral`, `abstain`, in both directions, on the already-admitted mDeBERTa NLI pin.
  2. `mention.same_referent`, on an open-weight instruct model chosen by a fixture spike.
  3. `recap.covered_by`.
  4. Supersession direction, relation typing and `term.same_meaning`.
- Add an **append-only, per-vault readings ledger** in the machine-local state root. It is durable: not in the vault, and not in the disposable dreamer sidecar. Every reading records:
  - its instrument identity;
  - input unit refs, with the sha256 of the exact text fed in and the extractor version;
  - the full probability vector for each direction;
  - the verdict, including abstain;
  - `sensed_at` and the placement.
- Specify **invalidation and migration**:
  - An edited input makes its reading stale, and the pair is re-sensed. Unit-level hashing means an unrelated edit re-senses nothing.
  - A label-map change re-derives verdicts from the stored vectors, with no sensing.
  - A new pin keeps old readings but consumes only the active pin's. It reports incomplete evidence until a lazy re-sense drains, open work first.
- Run inference in a **supervised, disposable sensor-worker child process**. The dreamer thread and request threads never load a model.
  - The worker inherits the dreamer's gates and adds its own CPU-seconds and judgements budgets per hour, at low OS priority.
  - Quiet mode and auto-quiet kill it, so its memory returns to the host.
  - Local sensing runs on CPU until co-tenant GPU pressure detection ships.
- Add **deterministic pair proposers** that read stored data only: unit vectors at a fixed cosine threshold per pair (never top-k), structural co-occurrence, and temporal same-subject pairs.
- Add **deterministic epistemic projections** that are pure functions of (ledger, graph, pages):
  - sensed typed unit edges;
  - contradiction components;
  - refinement and supersession chains in time order;
  - convergence across independent origins;
  - emerging connections to recent work.

  They live in their own disposable projection file, so they can never move the dreamer sidecar's size cap or touch a structural family. Replaying the ledger into a fresh projection yields byte-identical results.
- **Never collapse uncertainty.** Directional asymmetry, abstention and disagreement between instruments are each kept as their own state. Every served item names its verdict, probability, instrument and fixture precision.
- **Deliver pull-first.**
  - A point-of-use status line on activation and read, such as "refined by 2 later notes; 1 open contradiction". It never ranks and counts released pages only.
  - Tensions on active work ride the existing upkeep block under its caps.
  - Nothing new pushes.
- **Egress.** A reading is consumable for a caller only when every input unit is released. Counts, components and chains are recomputed from released edges. Selection is monotone per pair. A binding per-page sensing cap makes that page's sensed items owner-only.
- **Placement:**
  - Local models by default.
  - API instruments are an opt-in placement per vault, flagged `unpinned_weights`.
  - Cloud cells run the dreamer, with sensing on an in-cluster shared plane. No third-party API sees vault text by default.
- Move the audit's in-request NLI enrichment onto the ledger.
- **BREAKING (constitutional):** amend the canonical `frozen-verifiers` contract:
  - Generative instruments are admitted under a delimited-slot template with closed-label probability output, never sampled text.
  - Readings may originate sensed families, but never enter canon except through an agent-authored write that cites the reading.
- Rewrite `docs/hosted-inference-boundary.md`'s thresholds as acceptance measures rather than permission gates.
- Re-lane `close-memory-loop` task 8.4 here, together with its verifier non-use condition, and reverse its "hosted cells do not run the dreamer" clause. That clause's reversal is specified here and built in a later slice.

## Capabilities

### New Capabilities

- `sensed-epistemic-model`: instruments, question registry, readings ledger, invalidation and migration, the sensor worker, pair proposers, deterministic projections, pull-first delivery, per-caller egress, placements and evaluation.

### Modified Capabilities

- `frozen-verifiers`: admission extends from one stance classifier to closed-question instruments. It covers generative templates (R1) and API identity (R7). Readings may originate sensed families (R2) without entering canon. Instrument output lands in the durable ledger.
- `contradiction-queue`: the polarity label on proximity pairs is read from the readings ledger instead of being computed during the audit request.

## Impact

- **Code:**
  - new: `sensing`, `sensing_ledger`, `sensing_nli`, `sensor_worker`, `sensor_worker_child`, `sensed_model`;
  - `dreamer` (sensed projection inside the tick's budgets, and supervision in the loop), `commands` (read and activation status line). The stance verifier's pin and loader in `claims` are reused unchanged.
- **State:** a new durable file, `<vault state dir>/sensing/readings.sqlite`, and a new disposable projection, `<vault state dir>/sensing/projection.sqlite`. The dreamer sidecar's schema and delivery ledger are untouched.
- **Runtime:**
  - Sensing is default-off, and gated by `EXOMEM_SENSING` or the config key `sensing` (only with the dreamer on).
  - It soft-fails to byte-identical silence when the `nli` extra, the pin or its fixtures are unavailable.
  - The instrument's cost is bounded per hour, and quiet mode kills the worker.
- **Pure substrate:** instruments measure and never decide. Their readings are measurement records. The dreamer's projections are deterministic. Only the agent authors canon, through existing governed writers.
