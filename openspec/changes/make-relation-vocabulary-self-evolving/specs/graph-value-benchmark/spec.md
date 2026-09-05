## ADDED Requirements

### Requirement: Graph-value corpus covers relation vocabulary evolution

The product-neutral synthetic graph corpus SHALL include policy applicability requiring a vault extension, child-to-parent membership satisfied by core `part_of`, an existing extension reached through a different clean alias, a legitimately generic relation, topical proximity that must not become a directional claim, extension parent-family roll-up, survivor-directed deprecation replacement, and two isolated vault registries. The Exomem gate SHALL exercise each case through the public resolve, propose, save, graph, filter, and explain surfaces that apply. Because resolution is non-authoritative, the gate SHALL distinguish candidate evidence from an explicit scripted caller decision and SHALL verify that resolution alone performs no registry or Markdown mutation.

#### Scenario: Accepted applicability extension works end to end
- **WHEN** the synthetic organisation policy is connected to a specific employee case through reviewed `vault.applies_to`
- **THEN** the benchmark finds the edge by clean alias, exact canonical key, and parent-family filter
- **AND** raw identity, canonical identity, direction, provenance, and match mode are scored independently

#### Scenario: Existing and generic semantics are not penalized
- **WHEN** the membership, generic, and false-specificity cases run
- **THEN** resolution exposes `part_of` without proposing or writing, the scripted caller explicitly authors it, and an explicitly authored truthful `relates_to` passes
- **AND** topical evidence alone produces no selected or authored directional relation, while any scripted false directional choice scores as a false positive

### Requirement: Relation review has a scale and concurrency regression gate

The repository SHALL provide a deterministic model-free relation-review benchmark over at least 3,600 eligible synthetic pages and a workload of twenty concurrent request streams containing graph reads and mutations that change canonical Markdown on eligible graph pages. Validation-only calls, no-op edits, and graph-excluded writes SHALL NOT count as mutations. Every successful mutation SHALL advance a real graph checkpoint, and an explicit barrier SHALL prove queue reads overlap graph-relevant commits during the mixed phase. The harness SHALL report separate available and typed-unavailable queue latency distributions; mutation committed, busy, and failed outcomes; graph checkpoint generations; graph availability ratio; time from the final graph-relevant commit to a current available queue completion; graph snapshot and query counts; Markdown parse counts; embedding-call counts; and corpus-size-normalized candidate counts. A current-graph queue request for at most twenty source groups MUST use one snapshot, zero full-vault Markdown parses, zero embedding calls, and a bounded query count independent of corpus size. A passing calibrated Linux run MUST include at least two committed graph-relevant mutations, at least 90 percent available queue completions, and a current available queue completion within 5,000 milliseconds after the final graph-relevant commit. Available queue p95 MUST remain below 1,000 milliseconds and no available sample may exceed 2,000 milliseconds; typed-unavailable queue p95 MUST remain below 250 milliseconds. An all-busy mutation run, all-warming reader run, or non-overlapping mixed phase MUST fail.

#### Scenario: Concurrent review remains usable
- **WHEN** the calibrated scale fixture runs with twenty concurrent request streams
- **THEN** relation queue reads satisfy the availability, recovery, structural, and state-specific latency gates while some mutations may report legitimate retryable busy states
- **AND** at least two eligible-page Markdown mutations commit, each advances a graph checkpoint, their mixed phase overlaps queue reads, and the queue becomes current and available again within the recovery bound after the final commit
- **AND** no queue read triggers graph reconstruction, a Markdown census, or embedding materialization

#### Scenario: Semantic tests synchronize on behavior rather than tight clocks
- **WHEN** unit and integration tests exercise queue reads beside an injected held mutation boundary or graph publication
- **THEN** they assert snapshot isolation, bounded calls, and typed availability states using explicit synchronization
- **AND** absolute timing assertions remain confined to the dedicated calibrated benchmark
