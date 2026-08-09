# Give the benchmark a compiled ingestion altitude

## Why

Track B bulk-loads every corpus document as a raw source and never compiles
anything. Measured on the 2026-08-07 full-strength vault: **205 pages under
`Sources/`, zero compiled notes, `ingested_into: []` on 204 of 204 sources,
zero `derived_from` edges.** The benchmark evaluates the product in a state no
user's vault is ever in.

Three separately-filed defects collapse into that one cause:

- **`provenance` 0/208** (4b.31) — a citation chain is a compiled conclusion
  declaring the sources it drew from. Nothing compiled means no chain, so the
  dimension degenerates to "which documents did you return", which is the same
  shallow thing for every contender.
- **`contradiction_uncertainty` 0/20, floor 0 and ceiling 0** (4b.33) —
  detection runs over compiled conclusions that disagree. A pile of independent
  raw sources has nothing to detect.
- **`abstention` 0 declines in 236 queries** — a dense raw dump always matches
  something, so there is never a reason to decline.

None of those are product weaknesses, and each was investigated separately
before the shared cause was found. Two rounds of work — the native-answer seam
and then the attribution-surface plan — were spent trying to fix provenance
without touching the altitude, and neither could have worked.

The governing principle: **a benchmark's job is to be predictive, not
lifelike.** Numbers measured over a document distribution the product never
encounters do not transfer to real use, and that failure is invisible in the
results — which makes it worse than a visibly wrong gate.

This change adds a second, declared ingestion altitude in which the corpus
arrives through the lifecycle — capture, then compile — so the vault under test
has the shape a real vault has. It does **not** replace raw-source altitude:
both are legitimate and measure different things, and which one a run used is
already recorded (`ingestion_altitude`, shipped 2026-08-08).

**Pure-substrate justification (required by `openspec/config.yaml`): no model is
added anywhere.** The compile plan is derived entirely from the oracle, which
already knows every claim, the sources that assert it, and every supersession
and dispute edge. The harness performs a deterministic transduction of known
ground truth into each contender's native grammar. Nothing reasons; nothing
decides what is worth remembering. That limit is real and is stated as a
limitation rather than hidden: a scripted compile is more faithful than a bulk
dump and less faithful than an agent deciding what to keep. Only the
agent-in-the-loop tracks answer the latter.

## What Changes

- **Corpus.** Generation emits a deterministic, seeded **compile plan**: for each
  claim, a conclusion record naming its title, body, the source ids it draws
  from, and any conclusion it supersedes. Derived from the oracle, versioned and
  hashed with the rest of the corpus.
- **Harness.** A declared `compiled` ingestion altitude. Each adapter renders the
  compile plan in **its own native grammar** through the existing
  native-renderer seam — the mechanism already used to stop the harness
  measuring its own configuration. An adapter that cannot compile declares so
  and reports `unsupported`; it is never scored zero and never silently
  compared against one that can.
- **Scoring.** At compiled altitude, `provenance` measures chain preservation
  (does the system report the conclusion→sources link it was given) and
  `contradiction_uncertainty` measures whether conflicting compiled conclusions
  are surfaced. Both remain withheld at raw-source altitude.
- **Reporting.** Cross-altitude comparison is refused on altitude-dependent
  dimensions, as cross-mode and cross-governance comparison already are.

## Impact

- Affected capabilities: `memory-proof-corpus`, `memory-proof-harness`.
- **Changes generated corpus bytes.** Must land before `4.1` (replication kit)
  and `4.2` (versioned releases) pin a release, and should be regenerated
  together with `4b.32` (entity-name collisions) so the corpus is rebuilt once
  rather than twice.
- Existing raw-source runs stay valid and comparable to each other; they are not
  invalidated, only labelled.
- Relation to `expand-memory-proof-benchmark`: that change's fairness
  invariants — oracle-derived expectations, unsupported-never-zero, per-fact
  parity reports, no aggregate — bind this one unchanged.
