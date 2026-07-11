## Context

Exomem and Basic Memory both derive graphs from owned Markdown, but their native abstractions differ. Basic Memory indexes note entities, atomic observations, and open-vocabulary relations, then traverses connected entities through `build_context`. Exomem indexes files plus semantic blocks, resolves relations through a governed registry, records origin/anchor/hash metadata, and exposes bounded traversal profiles for epistemic, provenance, causal, decision, or all-relation context.

A comparison that feeds one product the other's syntax, counts raw edge density, or collapses every outcome into one score would be easy to game. The benchmark must instead start from product-neutral facts and tasks, render equivalent native corpora, invoke public graph behavior, and report each claimed advantage independently. No reasoning model is needed: the harness measures deterministic reachability and returned structure while the evaluator compares it with explicit expected facts.

## Goals / Non-Goals

**Goals:**

- Make “Exomem's graph is superior” a reproducible, falsifiable graph-only claim.
- Protect ordinary note-level reachability while testing the governed dimensions Exomem adds.
- Compare current product-facing behavior over equivalent semantics and native authoring grammar.
- Keep a fast model-free Exomem gate in normal tests and a direct persistent-MCP comparison for desk-side evidence.
- Produce failure details precise enough to drive narrow runtime fixes.

**Non-Goals:**

- No claim that Exomem is globally superior on onboarding, generic note search, canvas UX, or ecosystem breadth.
- No weighted composite, graph-density target, private-vault leaderboard, or benchmark-specific runtime branch.
- No server-side reasoning model, graph database migration, schema expansion, or new relation vocabulary.
- No required Basic Memory dependency in Exomem's package or lean CI.

## Decisions

### Use one semantic manifest with native corpus renderers

The benchmark manifest will define neutral note identities, facts, directed relations, lifecycle state, provenance targets, semantic-block anchors, distractors, and graph tasks. An Exomem renderer will express those facts through governed frontmatter, semantic blocks, `## Relations`, Sources, and Evidence. A Basic Memory renderer will use its documented frontmatter, observations, and open relation bullets.

This is preferable to sharing byte-identical Markdown, which would privilege one parser, or maintaining unrelated hand-written corpora, which would allow semantic drift. The generated trees will be hashed before and after contender runs to detect mutation.

### Normalize public outputs before scoring

Each contender adapter will return a small normalized result: reached note identities, typed directed edges, observations/blocks, provenance fields, lifecycle fields, response bytes, latency, contender version, and unsupported capabilities. Scoring will operate only on that contract, not internal SQLite tables.

Exomem's fast gate may call its shared graph leaf in-process because that is the implementation behind MCP/REST/CLI. The direct comparison will call both products through persistent MCP servers. The Basic Memory adapter will run from an explicit checkout or executable, use an isolated home/config/database, disable semantic search and file mutation, and record its version and git revision. It is default-off and returns actionable unavailable status rather than failing lean tests when the external contender is absent.

### Score independent graph dimensions

Cases will cover:

- note-level one-hop and multi-hop target reachability;
- distractor exclusion under bounded results;
- exact relation-type and edge-direction fidelity;
- traversal-lens filtering;
- provenance traceability including relation origin and source anchor;
- supersession and active-conclusion selection;
- semantic-block precision, including block kind/anchor and its relation;
- response bytes and latency as informational efficiency measurements.

Every case reports numerator, denominator, ratio, and concrete missing or unexpected normalized facts. Latency and response size remain visible but do not override correctness.

### Define superiority as dominance, not an average

The comparison passes only when all of these are true:

1. Exomem is no worse than Basic Memory on common one-hop and multi-hop reachability.
2. Exomem is no worse on distractor precision, relation-type fidelity, and direction fidelity.
3. Exomem passes every Exomem graph invariant in the fixture gate.
4. Exomem strictly exceeds Basic Memory on provenance traceability, supersession handling, and semantic-block relational precision.

An unsupported contender capability scores as unsupported/zero for that dimension with an explanation; it is never silently omitted. The report contains no overall weighted score, so a governance win cannot hide a reachability regression.

### Separate benchmark construction from measured fixes

The first run establishes the baseline. If Exomem fails a criterion, the same OpenSpec change may add a narrowly scoped graph fix only after the failure is recorded and a regression case exists. The fix must improve public graph behavior, not inspect benchmark identifiers or special-case fixture content.

### Keep reports aggregate and reproducible

JSON contains the manifest version, corpus hashes, contender versions, dimension totals, dominance checks, and sanitized case IDs. Markdown renders the same aggregate facts and reproduction commands. Neither format contains private paths, private note text, query text from a personal vault, or environment secrets.

## Risks / Trade-offs

- [Risk] Native renderers accidentally give one contender more semantic information. -> Keep facts in one manifest, test renderer parity, and publish the mapping table.
- [Risk] A small synthetic corpus overstates real-world value. -> Use adversarial distractors and multi-hop cases now; add a separate public medium tier later without changing metrics.
- [Risk] External Basic Memory setup drifts. -> Record the executable version, git revision when available, tool schemas, and generated corpus hash on every run.
- [Risk] Product adapters normalize away meaningful differences. -> Preserve unsupported fields and raw counts, and make normalization rules part of the report.
- [Risk] Cross-product latency is noisy. -> Treat it as informational and keep correctness dominance independent of timing.
- [Risk] The benchmark becomes a feature wishlist. -> Require a measured failed criterion and regression case before changing runtime behavior.

## Migration Plan

This is additive. Land the deterministic Exomem gate and report tooling, run the optional direct comparison desk-side, then publish the recorded report. Rollback removes benchmark files and any benchmark-justified runtime fix; no vault or sidecar migration is involved.

## Open Questions

None for the fixture tier. A public medium corpus can be added after the first direct comparison identifies which dimensions need more statistical depth.
