# Design — resolving a structural suggestion by state change

## The constraint that shaped v1, restated

v1 confined every reported fact to the page the caller just wrote, so that "nothing in the payload is something the caller who performed the write could not already see, so no disclosure decision is needed on the write path."

That rule governs what is **reported**. Resolution reports nothing — it is expressed purely as the absence of a suggestion. But silence is observable, so the rule cannot simply be waived: a caller who writes and sees the advisory vanish learns one bit about the existence of a page declaring that vocabulary.

Three properties keep that bit safe:

1. The destination set is drawn from `SemanticCorpusContext.eligible_compiled_paths`, the same eligibility the caller's own recall is subject to. A page the caller may not see does not clear the caller's suggestion.
2. The inference is about the caller's own vault, and in the case that matters the caller *created* the destinations.
3. Failure is open. No corpus, unreadable corpus, empty eligibility — the suggestion is emitted, never suppressed. Silence is never the fallback.

## Alternatives considered and rejected

**Dismissal, snooze, or per-page suggestion history.** Rejected outright, and not merely as scope. `f25 restructure_lifecycle` is explicit: "A product that quietly records a dismissal to clear its own suggestion fails here even though the queue looks identical." A `promoted_to` frontmatter field is the same thing wearing a hat, and would additionally be a nudge to record a nudge.

**Intra-page routing evidence — the origin page's own outbound wikilinks.** The attractive option, because link text is on the page the caller wrote and leaks nothing. It fails on the real data. The dogfooded origin page links exactly one destination, `Notes/Research/France Farm/france-family-farm-rural-holding-hub`, whose tokens are `france, farm, rural, holding, hub`. The fired cluster is `agriculture, cattle, champagne, financing, mortgage, property`. The overlap is **zero**, because a good hub name is a generalisation and cluster terms are specifics. Any lexical rule over link targets fails the same way. Rejected on evidence, not taste.

**Requiring the origin units to be removed.** This is current behaviour and is what makes the defect. Deleting durable units to silence an advisory destroys anchors, inbound relation targets and history; the advisory must not push the user toward that.

## The predicate

After the existing gates produce a cluster with recurring seed terms:

1. Build the destination index once from `corpus.pages`, restricted to `corpus.eligible_compiled_paths`, excluding the written page by path. Each destination contributes its declared identity — frontmatter tags, significant title tokens, and project keys — through the same `_terms` normaliser the detector already uses on the written page, so the two vocabularies are comparable by construction.
2. A destination **covers** a cluster term when that term is in its declared identity.
3. A destination **contributes** only when it covers at least `MIN_DESTINATION_COVERAGE = 2` cluster terms. This is the false-positive guard: in a large vault some page will carry almost any single tag incidentally, and term-by-term scavenging across unrelated pages would silence real divergence.
4. Routed terms are the union of the terms covered by contributing destinations. Remove them from the seed set and re-run the mass gate. If the remainder no longer reaches `CLUSTER_MIN_TERMS`, the cluster has a home and no suggestion is emitted.

Re-running the existing gate rather than adding a new threshold keeps one definition of "enough material to justify a child note", and means partial routing behaves correctly: routing half a cluster leaves the other half loud, which is the honest answer.

## Validation against the real case

Measured, not projected. The cluster the origin page carries has **21** recurring seed terms; the six in the payload are only the most recurrent, truncated by `MAX_CLUSTER_TERMS`. Resolution is evaluated against the full seed set, so the payload is a misleading thing to reason from — an earlier draft of this document did exactly that and got the answer wrong.

Against the six destinations the user created on 2026-08-15:

| Destination | Seed terms covered | Contributes |
|---|---|---|
| `france-family-farm-rural-holding-hub` | 8 | yes |
| `property-financing-and-land-strategy` | 7 | yes |
| `livestock-husbandry-breeds-and-products` | 5 | yes |
| `agricultural-qualification-grants-and-regulatory-path` | 5 | yes |
| `meat-milk-quality-and-future-research-agenda` | 3 | yes |
| `hospitality-family-business-and-diversification` | 1 (`farm`) | refused |

Routed: 16 of 21. Remainder: `countryside, income, ptz, rita, savings` — 5 terms against `CLUSTER_MIN_TERMS = 4`, so the page **still speaks**, and that is the honest outcome rather than a shortfall to tune away. The farm material that was promoted stops being reported. What remains is the household-financing thread — first-time-buyer scheme, income, savings — which the 2026-08-15 split genuinely did not route anywhere, plus two low-value terms (a person, and a generic locality word).

So this change converts a permanently stale signal into a smaller true one. It does not promise silence, and it should not: silence would require suppressing material that really has no home.

Two design consequences follow from the measurement:

- **Do not add a "mostly routed" ratio.** Clearing at, say, 75% coverage would have silenced this page and buried the unrouted financing thread. Re-applying the existing mass gate to the remainder is what keeps the answer honest.
- **The two-term rule earns its place.** It correctly refuses the hospitality note, which matches only on the generic `farm`, while every genuine destination clears it comfortably.

## Cost

The pass is deferred until a cluster has actually formed, so ordinary writes pay nothing at all — the overwhelming majority of writes return before the corpus is ever touched. On the rare write that would otherwise speak, the measured cost of the destination pass is 9.2 ms over 2,000 pages and 33.4 ms over 8,000, against a 750 ms median commit ceiling and observed commit medians of 137–168 ms.

That is a real, if small, addition on the loud path rather than the "inside run-to-run noise" the original detector could claim, and it is stated here rather than left for a reviewer to find. It buys the property that the loud path stops being permanent.

## Corpus availability

`ExistingPreflight` already carries `after_corpus`, so `edit_memory` and `observe_memory` need no new work.

`CreationPreflight` carries none, but `_evaluate_structural` already builds a corpus context during creation preflight and discards it. Retaining it is free — no second build, no extra walk — and avoids shipping a resolution that reaches two writers in four, the failure recorded in `exomem-write-feedback-never-reaches-the-default-mutation-response`.

For creation the retained context is the *before* corpus, which does not contain the page being created. That is correct: the written page is excluded from the destination set anyway.

## What stays unchanged

The payload keys, the reason codes, the strength values, the ordering and boundedness guarantees, every existing threshold, and the bare-exception guard around the whole analysis. `detect` keeps its current signature with the destination index optional, so a caller without a corpus gets exactly today's behaviour.
