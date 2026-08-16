# Design — due-state consumers and carriers

## Context

This is the keystone slice of the 2026-08-15 no-nudge architecture (KB: `Notes/Research/Exomem/no-nudge-architecture-sensors-verifiers-one-decider`): representation shipped without activation, and detection shipped without delivery. Two review rounds shaped this contract — the counters channel was originally specced against a cache that does not exist and a bootstrap-only trigger that is blind mid-conversation; both defects are designed out here rather than papered over.

## Decisions

**A maintained projection, not a per-call computation and not a cache of one.** The attention summary is rebuilt on every call today, and a counters block served on mutating responses would be invalidated by the very write that serves it — so "cache the attention result" is structurally the wrong design. The projection is instead maintained: incremental per-family deltas on write (a write can only affect the families its page participates in), day-boundary re-bucketing for time-driven transitions (a `check_by` passes at midnight with no write; generation tokens cannot see time), reconcile as the healer after out-of-band edits, full recompute as the recovery path. Persistence sits beside the review state and inherits the scaling obligations already recorded for that store.

**Registered advisory posture, following the structural-suggestion precedent.** The compact terminal drops unregistered advisory fields — that is why `write_feedback` never reached default MCP clients, a recorded failure. The structural-promotion change established the correct wire pattern: a validated, bounded, advisory projection lifted from the leaf, never a branch key, absent when empty. `due_state` uses that exact posture. No tool input schema moves, so the tool-surface fingerprint and connector attestations are untouched.

**Three carriers, one computation.** Bootstrap (session start), mutating responses (the moment the agent is provably attending — it just wrote), recall responses delta-only (reading turns are where "this prediction is due" naturally belongs; without this the channel is blind in read-only conversations). Hooks remain the stronger tier where they exist; this trio is the portability floor every client gets, and the floor is verified against real clients, not assumed — acceptance includes live thin-client probes, because a client that truncates tool results would make the floor fiction.

**Emission governance is part of the contract, not a UX nicety.** The top product risk of the whole programme is nagging, and an unconditional per-mutation attachment would be the system nagging about its own anti-nagging machinery. Change-of-count emission, no identical-total repeats, and batch-once are deterministic, agent-free, and tested — and the bench family f23's bulk-write assertion binds them.

**Egress before counting, everywhere.** A count is an aggregate, and the governance plane's silence rule extends to aggregates: a withheld item contributes nothing, and its absence is indistinguishable from nonexistence. Per-audience projections are computed on demand for the requesting audience and cached per audience; today's cells are effectively single-owner, so the multi-audience cost is a consolidation-era concern, noted for that programme.

**Unit-local prediction predicate.** Due = `check_by` past ∧ no `verdict` ∧ no resolving relation authored on the unit itself. Inbound resolution edges await fragment-preserving relation targets (a separate graph refinement); depending on them now would make the predicate quietly wrong. When that refinement lands, the predicate may widen — as a new spec delta, not a silent change.

**What this change refuses.** No ranking effect from due state (a "due first" sort is the authority-score failure through the back door). No new tools. No per-family suppression — that is the nag-governance slice, and conflating them would couple this change to the delegation-envelope decisions. No hosted scheduling. No model involvement anywhere.

## Alternatives rejected

Bootstrap-only counts (blind mid-conversation — the motivating dogfood case accumulated inside one session); hook-based delivery as the floor (hooks do not exist on the hosted, claude.ai, or ChatGPT surfaces where the floor matters most); synchronous audit inside the mutation path (violates the write-latency gates and the shortened critical section); an episode-boundary agent sweep as a substitute (adopted as taught behaviour, but it inherits instruction decay and misses mid-episode transitions — it composes with, and does not replace, the carriers).
