## Context

`SemanticPageState.body_wikilinks` already stores each deduplicated outbound body
wikilink as `(target, line)` while the page body is in scope. The current disposition
checks typed facts and then authored-relation connectivity facts, but it falls through to
reviewed-none without consulting that page-state measurement. The earlier attempt to turn
every body link into a `RelationFact` failed the 8k semantic write-latency gate.

The same-session Windows comparator produced:

| phase | pages | validate median | commit median | commit p95 |
|---|---:|---:|---:|---:|
| before | 2,000 | 34.3 ms | 133.0 ms | 138.3 ms |
| before | 8,000 | 167.9 ms | 335.7 ms | 520.5 ms |
| after | 2,000 | 32.5 ms | 140.3 ms | 153.2 ms |
| after | 8,000 | 135.7 ms | 383.1 ms | 407.1 ms |

Commit scaling was 2.52x before and 2.73x after. The after-run remains below the 750 ms
8k ceiling and below the approximately 2.75x scaling limit. The benchmark's unchanged
`measure()` and `check()` functions were invoked through a wrapper that pre-created the
private lock directory because this managed Windows sandbox denies the directory produced
by the benchmark's normal lock setup. Linux remains the authoritative acceptance gate and
must be run outside this sandbox, where WSL process execution is denied.

## Goals / Non-Goals

**Goals:**

- Recognize only resolved outbound body wikilinks to other existing connectable targets.
- Reuse the existing `qualifying_relation` disposition kind and report
  `qualifying_signal="connectivity"` with the existing warning.
- Preserve the latency gate through normalized set membership only.

**Non-Goals:**

- No `RelationFact` construction, registry lookup, inbound-edge scan, new disposition
  kind, or change to typed qualification.
- No widening of `eligible_governed_paths`, provenance qualification, or unresolved-link
  qualification.

## Decisions

### Resolve page-state targets directly against the connectable set

After `qualify_connectivity` finds no fact, `_relation_disposition` will normalize each
stored body-wikilink target into a canonical vault-relative path and test membership in
`SemanticCorpusContext.connectable_target_paths`. The first match returns the existing
`kind="qualifying_relation"` shape with `qualifying_signal="connectivity"`.

This deliberately duplicates only the cheap final membership decision. Reintroducing
wikilink facts would repeat fact construction, registry resolution, and target resolution
for every body link across the corpus, which previously pushed the 8k commit median to
1,259.3 ms.

### Keep body-wikilink evidence separate from relation facts

The disposition may name the matching target as its qualifying reference, but no fact is
added to the corpus outbound map. This preserves the typed/connectivity fact semantics and
keeps unregistered authored relation labels visible as relation debt rather than silently
downgrading them.

### Preserve existing boundary sets and fall-through order

The check runs only in the outbound connectivity branch, after fact-based connectivity
and before reviewed-none. `connectable_target_paths` remains distinct from
`eligible_governed_paths`, so captured Sources do not consume the bootstrap exception.
Frontmatter and inbound links never reach this check, while self, missing, ambiguous,
and inactive targets fail qualification or membership.

## Risks / Trade-offs

- Path normalization could drift from corpus indexing. Mitigation: use the repository's
  canonical wikilink/path normalization seam and test eligible, Source, inactive,
  non-connectable, and unresolved targets.
- A satisfied branch could accidentally introduce an unknown disposition kind.
  Mitigation: reuse the existing kind and pin `qualifying_signal` plus commit behavior.
- Corpus-scale iteration could regress latency. Mitigation: reuse the already bounded,
  deduplicated page-state tuple and measure 2k/8k before and after without fact creation.
- Linux verification is unavailable inside this managed Windows session. Mitigation:
  record the same-session Windows comparator and leave the authoritative Linux command as
  an explicit handoff gate rather than weakening or reinterpreting it.
