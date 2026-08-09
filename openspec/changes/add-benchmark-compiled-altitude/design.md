# Design — compiled ingestion altitude

## Context

Ingestion altitude is already declared and recorded (`raw_source` | `compiled`,
shipped 2026-08-08); only `raw_source` is implemented. This change implements
the other half. The fairness machinery it must fit into already exists and has
already caught real defects, so the design reuses it rather than inventing:

- **Native renderers** (`_NATIVE_RENDERERS`) — each contender receives the
  corpus rewritten into its own grammar. Registering only exomem meant the
  harness could structurally produce nothing but zeros for competitors
  (found 2026-08-05). This is the fairness mechanism.
- **Three-state governance** — score everything, exclude at report time with
  the reason visible, never a silent zero.
- **Unsupported-never-zero** — a capability a contender lacks is reported, not
  scored against it.

## Goals / Non-Goals

**Goals.** Make the vault under test resemble a real vault. Make `provenance`
and `contradiction_uncertainty` measure something. Keep the whole path
deterministic and model-free. Let every contender be measured the way it is
meant to be used.

**Non-Goals.** Replacing raw-source altitude (both stay). Simulating an agent's
*judgment* about what deserves remembering. Making Track B carry behavioural
claims — those belong to the agent-in-the-loop layer.

## Decision 1 — the compile plan is oracle-derived and lives in the corpus

Generation emits `compile-plan.jsonl` beside `claims.jsonl`. One record per
compiled conclusion:

```
{ "conclusion_id": "CON-…", "title": …, "body": …,
  "cites": ["SRC-…", …],          # sources that assert the claim
  "supersedes": "CON-…" | null,    # from the claim's supersession chain
  "disputes": ["CON-…"]            # conclusions asserting an incompatible value
}
```

Every field is computable from records the oracle already holds:
`required_citations` gives `cites`, supersession chains give `supersedes`,
disputed claims (t07 authority conflict, t08 equal-authority dispute) give
`disputes`.

**Why in the corpus rather than in the adapter.** It has to be identical for
every contender, hashed into the release manifest, and reproducible from a
seed. An adapter-side plan would let each product be handed a different set of
conclusions, which is the renderer defect wearing a new hat.

**Alternative rejected — compile at query time.** Cheaper, but the vault would
differ per run and nothing would be reproducible.

## Decision 2 — each contender renders the plan in its own grammar

The plan is neutral. Each adapter turns a conclusion record into whatever its
product calls a compiled conclusion, through the existing native-renderer seam:

| contender | rendering |
|---|---|
| `exomem-local` | `remember(title, body, sources=[…])`, then `replace_memory` for `supersedes` |
| `basic-memory-local` | `write_note` with relations to the cited source notes |
| `graybox-local` | declares it cannot: its compile step is an LLM `organize` pass, excluded by the model-free rule |
| `oracle-retrieval` | compiles exactly the plan — the ceiling for chain preservation |
| `null-abstain` | ingests, compiles nothing, retrieves nothing — the floor |

**This is the fairness requirement, and it is the part most likely to go
wrong.** Modelling the neutral record on exomem's `remember` signature would
hand exomem a native fit and everyone else a translation, reproducing the 2026-08-05
defect with the sign flipped. The record must carry only what any knowledge
store has: a conclusion, its cited sources, and its lineage.

## Decision 3 — cannot-compile is declared, never scored

An adapter declares whether it can honour the compiled tier. One that cannot
reports `unsupported` for altitude-dependent dimensions and is excluded from
compiled-altitude comparison. It is never scored zero, and a run that cannot
apply the tier to a contender refuses the comparison rather than quietly
comparing unequal configurations — the 4b.29 rule, applied to altitude.

`graybox-local` is the live case: it is honest about needing a model, and the
deterministic layer excludes model-driven compilation by construction. Reporting
that as a zero would be exactly the defect this benchmark exists to avoid.

## Decision 4 — what the two dimensions measure once compiled

**`provenance` becomes chain preservation.** The system was handed a conclusion
citing specific sources. Does it store and report that link through its own
provenance surface? Recall = required sources present in the reported chain;
precision = reported sources ⊆ the oracle-permitted set. Scored against the
product's own attribution surface, not a harness-authored answer — which is
what 4b.31 concluded and could not act on at raw altitude.

**`contradiction_uncertainty` becomes conflict surfacing.** Two compiled
conclusions assert incompatible values for the same claim. Does the system
surface the conflict when asked? Behavioural, not structural: no numeric
confidence field is required, consistent with the product's declared refusal to
carry confidence floats.

Both stay withheld at raw-source altitude. `abstention` is deliberately not on
this list — it is affected by altitude but measurable at either, and listing it
would overclaim.

## Decision 5 — one corpus regeneration, not two

This change and `4b.32` (18 colliding canonical entity names; three prompt pairs
byte-identical with mutually exclusive expected answers) both change generated
bytes. They land together and the corpus is rebuilt once. Regenerating twice
would invalidate every number recorded in between for no benefit.

## Risks

- **The neutral record leaks one product's shape.** Mitigated by writing the
  basic-memory renderer *first* and letting it constrain the record's fields.
- **Compiled altitude flatters exomem.** It should — that is the shape exomem is
  built for — but the ceiling contender guards it: if `oracle-retrieval` cannot
  reach ~100% on chain preservation, the gate is wrong, not the contender.
- **A compiled corpus is slower to build.** Acceptable; generation is not on the
  latency path.
- **The simulation limit is misread as realism.** Stated in the proposal, and
  every compiled-altitude figure carries the altitude label.
