<!-- authority:non-specification -->

# Context-activation benchmark runbook

Operational companion to OpenSpec change `add-context-activation-benchmark`.
Not a specification authority: the normative source is
`openspec/specs/context-activation-benchmark/spec.md` and
`openspec/specs/epistemic-utility-regression/spec.md` once the change is
archived (today, the change's own
`specs/context-activation-benchmark/spec.md` and
`specs/epistemic-utility-regression/spec.md`). This page is how to actually
run the instrument in the order the pre-registration requires.

## What this instrument is, and is not

- The **deterministic activation audit** (Layer A) is model-free, runs in
  CI over the seeded synthetic corpus, publishes no comparative claim, and
  carries no epistemic-bench registry row. It is implemented in
  `benchmarks/membench/utility/context_activation.py`, over fixtures in
  `epistemic.corpora.context_activation`.
- The **agent-in-the-loop arms** (Layer B, A1-A5) ride the released `f32
  utility_action_episode` family as new variants (a code-tuple extension,
  no new amendment). They are implemented in
  `benchmarks/membench/utility/context_activation_arms.py`, reusing
  `epistemic.journeys.f27_replay`'s isolation primitives directly. **No
  code in this repository executes a real `claude -p` session for these
  arms.** Building and printing a dry-run argv is the supported entry
  point; actual comparative replays are deferred, optional, and require a
  bounded authorized run under the existing paid-probe rule
  (`epistemic-utility-regression`, "Paid probes are bounded and opt-in").
  They are not prerequisites for delivering deterministic compiler acceptance
  and ordinary-use evidence. A future subscription-backed Codex runner must
  record returned usage separately from metered API costs and must not silently
  fall back to API billing.
- The **private real-vault instrument** runs the same Layer A scorer
  locally over a digest-pinned snapshot of a real vault. Its results are
  Evidence in the owner's own knowledge base, never committed here.

## Order of measurement (design.md D8)

Run these in order. Each step either produces the input the next step needs,
or exists specifically to strike cases that would make every later step
meaningless.

1. **Naive-path latency baseline.** Measure the deterministic baseline's own
   `ask_memory` latency on a quiesced cell before pinning any latency
   threshold. This is the measured constant behind the spec's two
   pre-registered latency bounds: the compiler's own `working_set.*` stages
   at p50 at most 800 ms and p95 at most 2,500 ms on the reference corpus,
   and end-to-end `activate_context` no slower than the measured naive-path
   baseline on the same cell (p50 10,745 ms / p95 16,488 ms over 18 nonce
   queries on the personal cell on 2026-09-16, recorded in the fixture
   manifest) -- the end-to-end bound to be tightened once
   `accelerate-governed-recall` lands.
2. **A5 ceilings.** Oracle-packet dry runs (or, once authorized, replays)
   per case. "A case for which A5 does not beat A1 SHALL be struck from
   the report and never scored against the compiler" — striking unwinnable
   cases here means every later step's denominator is honest.
3. **A1 floor.** The no-Exomem control, per case.
4. **A2/A4.** Raw recall, then nudged recall. If A4 already clears the
   pre-registered bar, that is the cheapest possible falsification of the
   compiler's claim (no A3 session needed to reach a verdict).
5. **Deterministic baseline.** `ask_memory` output, per fixture turn,
   labelled against gold/poison — this is the one step in this list this
   change actually runs (see below); everything upstream of it in this
   ordering that touches a real agent session is dry-run-only until
   separately authorized.

## Quiesced-cell and nonce rules

Two Exomem services may be live on this host (a personal cell and the second
client cell). Before any measurement against either:

- **Check the cell is quiet first.** Read instantaneous CPU from
  `/proc/<pid>/stat` deltas (two samples a second or more apart), never
  `ps %CPU` (a load-average-style figure that answers a different
  question). Do not measure while the cell is busy.
- **Use the normal client path only.** MCP (`ask_memory`) or the personal
  REST facade. Never an out-of-process `exomem index` or `find` run
  against a live service's state directory -- that measures a different
  code path and can corrupt an index a live service is using.
- **Never restart the cell, and never read its state directory directly.**
- **Put a nonce in every query.** A short, unique, per-query token appended
  to the turn text, so repeated measurement cannot be served from a cache
  and so a query is identifiable in the cell's own logs without needing to
  read vault content back out.
- **Titles and paths only, never quoted body content**, in any report this
  produces, whether committed or handed to the orchestrator.

## Repeats: n = 1 baselines, n = 5 comparison

- **Pre-implementation baselines run at n = 1 per arm per case** for A1,
  A2, A4 and A5. This is what "the first paid smoke" language in
  `PREREGISTRATION.md` §4 describes for the sibling `f32` smoke, applied
  here to the four arms that do not depend on the compiler.
- **The A3 comparison runs at n = 5 per arm per case**, reporting
  individual and modal outcomes -- never a mean across cases -- once the
  compiler (`add-context-activation`) exists.
- **Arm order rotates by seed and case index**
  (`context_activation_arms.rotate_arm_order`).
- Harness faults (non-zero exit, an error-subtype or `is_error` result, a
  malformed transcript line) are reported **blocked**, never scored as a
  loss (`context_activation_arms.harness_fault_status`).

## Stopping criteria (verbatim from the spec)

From `openspec/changes/add-context-activation-benchmark/specs/
context-activation-benchmark/spec.md`, "Run protocol, stopping criteria and
manifests":

> The mechanism SHALL be reported falsified if A3 fails to beat A4 on the
> reminder test in at least five of nine cases, if any twin yields a
> `resolved` false activation, if A3 uses a poison fact where A2 and A4 used
> none, if the no-memory case injects any context, if the supersession case
> presents superseded knowledge as current, or if A3 harms more than one
> case the control arm got right; accepted for v0 only if A3 beats A4 in at
> least seven of nine cases with every deterministic threshold met, zero
> poison use and a p95 packet at most 1,500 tokens; and indeterminate
> otherwise. The no-memory case counts as a win for A3 only when A3 answers
> correctly with zero injected characters and zero memory searches while A4
> searched or injected. Because the padded case shares the grill case's
> query, the bar of seven tolerates the loss of at most one distinct query
> and never the grill query; the report SHALL state that reading beside the
> count. Every run manifest SHALL carry the fixture-set digest, the corpus
> digest and the threshold digest; a manifest missing any of them SHALL void
> the run. All eighteen fixtures SHALL run or no verdict SHALL be published.

And, from "Agent arms and controls":

> A case for which A5 does not beat A1 SHALL be struck from the report and
> never scored against the compiler.

And, from "Pre-registered thresholds" (the C9/T9 corpus-tree and precision
rules this runbook's own naive-latency step and `score_padding_robustness`
implement):

> Every fixture and every packet SHALL record the corpus tree (its
> distractor count) it belongs to, and a padding comparison whose two
> packets share a tree SHALL fail. [...] activation precision at least 0.80,
> computed over every ref the packet surfaces as a resolved anchor, unit or
> pointer and excluding superseded ancestors the packet credits as marked
> [...] a current-state statement of at most 200 characters, a longer one
> failing the case as a packet-contract violation [...] percentiles taken
> ceil-rank so that over the eighteen packets of one run the p95 bound is
> the run's maximum and the hard refusal is reached only by larger runs.

## Running the deterministic audit (CI, always available)

```
uv run python -m pytest tests/test_context_activation_fixtures.py tests/test_context_activation_audit.py tests/test_context_activation_product_corpus.py -q
```

The public synthetic corpus is a real initialized Exomem vault. Its fixture
keys resolve under `Knowledge Base/` to governed entity and note pages,
Records collections and items, Planning collections and items, product/system
resources, and tagged hub notes. Construction uses the same typed writers as
the product; a fresh `WorkingSetIndex` must publish entity, hub, resource,
plan, and collection anchors before the corpus is eligible for compiler
quality measurement.

`build_corpus` runs that writer recipe in an isolated child process. The child
keeps the caller's `HOME` but clears inherited `EXOMEM_*` values and owns fresh
external state, configuration, lease, ledger, log, and temporary roots. A
writer refusal fails the build; curated gold resources are never substituted
with direct fixture file writes. Graph scheduling is disabled in this corpus
construction child because compiler graph publication belongs to the later
full golden gate.

`CorpusManifest.corpus_hash` binds the exact canonical Markdown, YAML, and
JSON bytes for one constructed snapshot, excluding generated navigation and
activity-log pages. Writer-minted identities and audit receipts therefore
change it. `CorpusManifest.logical_hash` is the separate reproducible fixture
identity: it covers the fixture set, seed, logical paths, and authored page
semantics while excluding only writer receipts already bound by the exact
hash. Run reports serialize both fields. A new corpus version invalidates reports
from earlier corpus bytes; historical reports remain unchanged.

Before activating a product corpus, call `freeze_reference_binding(root,
corpus_manifest, key_to_ref)` with the trusted canonical reference map. The
factory verifies the snapshot digests and reads the declared state sources from
canonical pages. Its frozen result binds each eligible `#current` reference to
its authored state and source identity. Pass that result as `reference_binding`
and record its `digest` as `RunManifest.reference_binding_digest`. Missing,
stale, or mismatched binding identities void the run.
Only `mechanism="oracle_packet"` and the legacy `"unknown"` mechanism allow
identity-only scoring without a binding. Every other mechanism requires both
the binding and its digest, including historical product mechanism labels.
Neither binding-optional mechanism establishes product acceptance.

Every distinct surfaced reference still counts in precision. A valid derived
state reference earns its own relevance credit without adding another gold
identity to recall. Packet provenance cannot create eligibility, arbitrary
fragments remain distinct, and known poison projections remain poison even
when their packet metadata is malformed. Identity-only oracle tests remain
supported separately; they do not establish product-path acceptance.
Projection dates must agree across the frozen source, unit `updated`, state
entry, and provenance; a missing or contradictory unit date earns no credit.

The audit itself is a library, not (yet) a standalone CLI:
`membench.utility.context_activation.run_audit` takes a `case_id -> packet`
mapping (from `load_packet` on an oracle-packet file, or from
`activate_context` output) and a `RunManifest` carrying the three required
pre-registered digests plus the required `logical_corpus_digest`, and returns
a report with no aggregate field -- every metric is a
per-case or per-case-per-anchor-kind numerator/denominator pair.

Product packets may carry `missing` and `ambiguity` labels as JSON objects.
The loader preserves every field as stable, sorted, compact Unicode JSON for
budget and fact-text checks, while retaining each ambiguity object's non-empty
`ref` as its scoring identity. Legacy string labels remain supported. An absent
field remains compatible with older packets; a present field must be an array,
including when empty, and malformed entries or `null` are rejected.
A `case_id` with no supplied packet is blocked; it is not evidence of a disabled
compiler. For the product mechanism-removal check, repeat the actual compiler
calls with `EXOMEM_DISABLE_WORKING_SET=1` and retain their returned packets.
Every positive case must fail under that intervention. Synthetic
`DISABLED_PACKET` scorer tests exercise the scoring rule only.

## The real-compiler run and its recorded report

`membench.utility.context_activation_product` is the product path, and
`tests/test_context_activation_real_compiler.py` runs it in CI. For each of the
two corpus trees it builds the corpus through the supported writers, publishes
the activation index, the lexical catalogue and the epistemic graph, refuses a
corpus whose entity, hub, resource, Records or Planning structures did not
publish, and freezes the reference binding. Only then does it call
`commands.op_activate_context` once per fixture, on the tree that fixture
names. References are the producer's own: an entity or hub by its memory ref,
checked against the page's `exomem_id`, and every other page by its path.
Each tree is scored under its own manifest (`mechanism:
product_activate_context`) and binding, then the eighteen scores are read as
one report. The scorer and its thresholds are unchanged.

The reproducible part of that run is recorded in
`docs/benchmarks/context-activation-product-2026-10-v5.json` (corpus v5; the
v4 report `context-activation-product-2026-09-v4.json` and the v3 report
`context-activation-product-2026-09.json` are kept as history):
fixture-set digest
`9d15d155…`, threshold digest `7b2785cf…`, the logical corpus digest of each
tree, per-case duals, the mechanism-removal outcomes, the topology findings,
and the amended columns (A2+A4, A7, A8, A9 and v5) beside the raw one.
Exact corpus bytes carry writer-minted identities, so each run's manifest
carries its own exact digest and binding, and the recorded report binds the
logical ones. The test fails when the recorded report no longer matches a
fresh run. After a deliberate product or corpus change, re-record it:

```
CONTEXT_ACTIVATION_RECORD_REPORT=docs/benchmarks/context-activation-product-2026-10-v5.json uv run pytest tests/test_context_activation_real_compiler.py -k recorded_report
```

### Round 6 on corpus v5 (2026-10-06): carried-page naming, 10/18 raw, 12/18 under A8, 14/18 under v5

Three compiler changes and no fixture, threshold or scorer line:

- **M1.** A page the retrieval carry admits and the turn names by two or more of
  its own title words resolves through the existing soundness rule, with
  `[lexical_overlap, retrieval]`. A page reached by a body phrase or one title word
  stays `retrieval_carried`.
- **E1.** An entity that a carried page's served unit names, and that the page
  links, is listed `partial` on `carried_link` with `via` naming the page. It never
  resolves. Each page lists at most two, first named first; a row the caller may
  not see, or one an earlier carried page listed, takes no slot.
- **E2.** A path, URL or remote in the turn contributes no words to subject
  evidence; a `./` or `../` path keeps only its file name. Any other slash run,
  such as `records/staging/prod` or `Node.js/React`, stays prose.

| Case | Raw | A8 | v5 | Change |
|------|-----|----|----|--------|
| C4 | red | pass | pass | the failure note resolves by its title and lists its colleague |
| C8 | red | pass | pass | the current approach note resolves by its title |
| T8 | red | red | pass | the support rota page resolves by its title |
| the other fifteen | same | same | same | unchanged |

Raw is 10, A8 12 and v5 14. Raw still fails C4, C8 and T8 on precision 0.50,
because it reads a gold page's own unit fragment as a second ref. M1 carries the
weight: with its title rule removed, v5 falls to 11 and C4, C8 and T8 fail again.
E2 moves no case on this corpus.

### Round 5 on corpus v5 (2026-10-05): the v5 instrument, 10/18 raw, 11/18 under v5

No compiler line changes. Hugo ruled three instrument corrections on 2026-10-05
(design.md D9, A4, A10); the raw v4 scorer stays computable and is reported
unchanged beside the new `amended_v5` column.

- **S1.** A `<page>#unit-` fragment whose parent is in the frozen canonical parent
  map counts as that page in recall, precision, poison and the twin rule. Unbound
  fragments stay distinct. Raw never mapped a served unit to its page, so serving
  the right unit halved precision or tripped the twin rule.
- **T6.** A twin expected `unresolved` that abstains with only `partial` anchors is
  the run's one allowed hedge, as the spec pre-registered. Two exceed the ceiling;
  any unit or pointer disqualifies it. The product's partial-only packet abstains
  and reads `unresolved`, so the raw hedge (observed `partial`) never fired.
- **T1.** Re-authored for corpus v5 ("I keep hitting the snooze button again this
  week."): no knowledge page records anything it names, so it leaves A10's list.
  Its v4 turn was answered by C1's poison page.

The same real-compiler packets scored three ways. On the v4 fixtures (T1 as it was)
raw is 9, A8 10 and v5 10. Every case green on raw stays green on v5, and no case
goes red under any column. A8 passes C8 and v5 does not, by design:

| Case | Raw | A8 | v5 | What v5 changes |
|------|-----|----|----|-----------------|
| C1 | red | red | red | precision 0.67 cleared; recall 2/3 and `capacity ceiling` remain |
| C4 | red | red | red | precision 0.50 cleared; recall 1/2 and the status remain |
| C8 | red | pass | red | precision 0.00 cleared; status `unresolved` remains (v5 has no A8 status clause) |
| T6 | red | red | pass (hedge) | the lone partial grill candidate is the hedge, not poison |
| T8 | red | red | red | the twin's own unit is no false activation; status remains |
| T1 (v4 turn) | red | red | red | unchanged: the turn is answered by a fixture page |
| T1 (v5 turn) | pass | pass | pass | valid negative twin |
| the other nine | pass or red | same | same | unchanged |

On corpus v5 the passing sets are raw 10 (the nine and the new T1), A8 11 (adds C8)
and v5 11 (adds T6). The audit stays red under every column: C1, C2, C3, C4, C8,
C9 and T8 fail for the status or recall reasons in the recorded report. `must_include`
stays case-sensitive (S2 was not approved), so C2, C3 and C9 stay red on their
lowercase facts even if their retrieval moves.

### Round 4 on corpus v4 (2026-09-28): recall breadth, 9/18 raw, 10/18 under A8

The recall-breadth round changes the product only: no fixture, threshold or
scorer line. Its rules come from three incident shapes seen in live use, none of
which the eighteen fixtures contain, so each is measured by a generalisation
test with invented names rather than by a case:

| Shape | Rule | Test |
|-------|------|------|
| A turn names two domains and one is dropped | Same-kind anchors the turn spelled apart (every member spelled, no shared token) are concurrent, not competing senses; a page a turn names by its own phrase is carried beside what it resolved; phrases naming different pages carry each (at most three) | `test_working_set_concurrent_contexts.py`, `test_working_set_named_domains.py` |
| A follow-up about a page listed in this conversation's recent context abstains | The caller's own session tier is a candidate for a turn that resolved nothing, gated by name overlap; served `partial`, `carried_by: follow_up` | `test_working_set_thread_overlap.py` |
| The entity resolves and its settled conclusions are not served | An entity anchor also reads the decision, insight and finding units of the pages linked to it (`precedents` defaults to entity anchors) | `test_working_set_entity_conclusions.py` |

Two rules follow from reading the red cases:

- **A carried page is read through the lenses its own units answer.** C8's note
  filed its observation under a category the sixth-priority lens cut off, so the
  carry read nothing. The lenses are now those that select what the page holds,
  chosen from one indexed category read (`lexstore.unit_categories_of`, about a
  millisecond; a unit-query probe cost 150-190 ms and was replaced).
- **A page is named by its title when one distinctive word sits beside an
  ordinary title word** (T8's "support rota"). Only when no two distinctive
  words named a page, and only where a current page's own title carries both
  words.

Per-case result after each mechanism, raw (with gold pages reached) and, where
it differs, amendment A8. Measured with the harness's own `activate` and scorer
on both trees, and matched by the recorded v4 report re-recorded with the real
o200k tokenizer (raw 9/18 and A8 10/18, beside the 9/18 base). Recall bought
some precision: C1 serves one more page outside its gold (precision 1.00 to
0.67, still red), and C8 and T8 serve their gold page as a carry, which raw
precision does not count (0.00; A8 counts it):

| Case | base (#1440) | +M1 same-kind | +M2-M4 named domains, thread, entity | +M5 lenses | +M6 title-named |
|------|------|------|------|------|------|
| C1 | red 1/3 | red 1/3 | red 2/3 | red 2/3 | red 2/3 |
| T1 | red | red | red | red | red |
| C2 | red 1/2 | red 1/2 | red 1/2 | red 1/2 | red 1/2 |
| T2 | pass | pass | pass | pass | pass |
| C3 | red 0/2 | red 0/2 | red 0/2 | red 0/2 | red 1/2 |
| T3 | pass | pass | pass | pass | pass |
| C4 | red 1/2 | red 1/2 | red 1/2 | red 1/2 | red 1/2 |
| T4 | pass | pass | pass | pass | pass |
| C5 | pass | pass | pass | pass | pass |
| T5 | pass | pass | pass | pass | pass |
| C6 | pass | pass | pass | pass | pass |
| T6 | red | red | red | red | red |
| C7 | pass | pass | pass | pass | pass |
| T7 | pass | pass | pass | pass | pass |
| C8 | red 0/1 | red 0/1 | red 0/1 | red 1/1 (A8 pass) | red 1/1 (A8 pass) |
| T8 | red 0/1 | red 0/1 | red 0/1 | red 0/1 | red 1/1 |
| C9 | red 1/2 | red 1/2 | red 1/2 | red 1/2 | red 1/2 |
| T9 | pass | pass | pass | pass | pass |
| **raw / A8** | 9 / 9 | 9 / 9 | 9 / 9 | 9 / 10 | 9 / 10 |

No positive case flips raw: C8, C4, T8 and C1 are read as `unresolved` or fail
precision on fragment refs, which is what amendment A8 (merged from the
benchmark branch) addresses; C8 passes under it. C1 reaches two of its three
gold pages (the weekly-limit note is carried beside the resolved collection);
the capacity-ceilings note shares no word with the turn and no link with the
collection. The negative controls C6, T2 and T9 pass and are still load-bearing:
the naming-gate removal now targets the per-phrase seam.

**Evaluated and not shipped: a subject-tag relation.** C1's missing relation
from a collection to its notes is a shared tag (`subscriptions`). Relating a
note to a resolved anchor because it carries a tag equal to a word of the
anchor's name, when at most six pages carry that tag, was built and measured:

- Applied to every anchor kind it regressed T3 and T7, whose poison notes carry
  a tag equal to a shared title word ("roadmap", "search").
- Restricted to collections it left T3 and T7 alone and brought C1's third gold
  note, but the corpus's ordinary "subscription audit" note (a streaming
  service) carries the same tag, so C1's precision under A8 fell to 0.67, below
  the 0.8 floor, in the very case the rule targets.

A tag is a filing habit, not a relation, so the rule cannot separate a note about
the anchor from one that shares its word, and it is not in the product.

Activation latency, three interleaved pairs (base is #1440), 18 fixtures times
five rounds, cold caches per call, ceil-rank percentiles:

| Pair | Base ws p50 / p95 (ms) | Head ws p50 / p95 (ms) | Base total p50 / p95 (ms) | Head total p50 / p95 (ms) |
|------|------|------|------|------|
| 1 | 104.5 / 167.6 | 111.8 / 175.7 | 127.6 / 1202.7 | 123.9 / 1252.7 |
| 2 | 109.0 / 174.3 | 112.2 / 156.0 | 130.4 / 1147.3 | 120.1 / 1203.0 |
| 3 | 106.5 / 152.4 | 110.3 / 169.9 | 123.6 / 1062.0 | 121.4 / 1124.9 |

Working-set p95 moved by +4.8%, -10.5% and +11.5% (mean +1.5%), inside the 22 ms
spread between the three base runs; total p95 moved by +4.2%, +4.9% and +5.9%
(mean +5.0%), inside the +10% bound. Total p50 fell about 5%: a carried page is
now read through the lenses it can answer instead of six that read nothing. A
resolved turn pays one extra query (about 12-15 ms) to read what else it named;
a carried turn pays an indexed category read of about a millisecond.

### What a resolved entity's project already settled (standing precedent)

Three rules from a field incident (a person entity resolved, yet the conclusion
page about that person's panel and the project's standing methodology page were
missing, and a scoped claim was cut before its qualifier). Tests use invented
names (`test_working_set_standing_precedent.py`); each rule is a spec scenario
in `close-memory-loop`.

| Shape | Rule |
|-------|------|
| A person's own conclusion shares no word with the turn and sits past the entity's 40-link cap | Inbound wikilinks past the cap are kept under their own relation (never part of the neighbourhood), and up to six of the entity's linked pages that hold decision, insight or finding units are read under `precedents`, newest first |
| The project's method page constrains any claim in the domain but is never named | A page declares itself standing with frontmatter `standing: true` and its own `project`; the packet serves it under `precedents` for a resolved anchor in that project, one page per project, two per packet, two units per page, ahead of the role's other units and outside its item cap |
| "chronic X only" lost to a 360-character cut | A unit is served whole up to 900 characters; a longer unit is a `unit_too_long` pointer, never a half-claim |

Real-compiler deltas: none. The recorded v4 report is unchanged (raw 9/18, A8
10/18) because the corpus declares no standing page, no entity there has more
inbound links than the cap keeps, and no served unit exceeded 360 characters.
The negative controls (an unlinked conclusion, a linked page with no
conclusion, another project's standing page, a second standing page in the
same project, a turn resolving nothing in the project) stay out, each pinned
in the new test module.

Review round (independent review of this branch): a referent only recency
supplied is skipped inside the precedent reach (and a recency-only project is
not read for conclusions); the entity's conclusion pages are date-sorted before
the cut; a standing unit is capped at `MAX_UNIT_CHARS` (a pointer past it, no
900-character allowance); the resolved anchor's material ranks ahead of items
carried beside it (`provenance.carried`, rendered as a `carried` line in the
hook for three bytes). Injected size, the hook's rendered block, median / p95,
base is `origin/main`:

| set | base bytes | head bytes | base tokens | head tokens |
|-----|------|------|------|------|
| corpus (16 fixtures) | 729 / 1118 | 749 / 1118 | 187 / 280 | 202.5 / 280 |
| continuity (4 cases) | 1046 / 1262 | 1046 / 1262 | 262 / 353 | 262.5 / 353 |
| project with a standing page (2 turns) | 433.5 / 482 | 615.5 / 749 | 106.5 / 118 | 154.5 / 191 |

The project fixture is the case the new rules add to: about 180 bytes and 48
tokens more per turn that resolves the project, for the standing method unit and
the entity's conclusion. The corpus grows by a carried page's line where a turn
names a second page.

Activation latency, three interleaved base/head pairs (base is the previous
head), 16 fixtures times five rounds, cold caches per call, ceil-rank
percentiles: working-set p95 moved by +6.5%, -3.5% and +0.5%; total p95 by
+1.9%, +0.3% and +1.2%, inside the +10% bound.

### Round 3 on corpus v4 (2026-09-28): red, 9/18

The activation-quality round (close-memory-loop, context-activation ADDED
requirements) changes the product, never the fixtures, thresholds or scorer.
T3, T4, T5 and T7 now pass, raw and amended:

| Case | Raw | What changed |
|------|-----|--------------|
| T3 | pass | A plan anchor reports its item's own page; the collection stays its home (task 6.12) |
| T4 | pass | A bare first name two unlinked people share is `ambiguous`, and the carry is not asked; a shared word forms that question only for people, or when a cased turn capitalises it away from a sentence start |
| T5 | pass | A unit the anchor's own lede already says is not served again as a fragment |
| T7 | pass | The contiguous run "AI search feature" strictly contains the run naming the market hub, so the turn narrows to the feature hub; a word of the wider name said elsewhere narrows nothing |
| C3 | red | The item ref is served, and the turn's words reach the other workstream's item (now a partial poison anchor) |

Under amendments A2+A4, A7 and A8 alone, the passing set is the raw one: A8
credits C4's carried failure note, but the colleague's entity is still never
reached (recall 0.50). The other red cases keep their round-2 reasons. C8's
carry now names the current head alone (retired revisions, by status or by
`superseded_by`, no longer count toward a word's rarity),
but no units-lane role reads a `current state` observation off a carried page.
A carried page is `retrieval_carried`, never `resolved` (design D3), and its
units are fragments (D9), so no positive case whose gold is an ordinary note
can pass through the carry. That is recorded for a ruling rather than worked
around.

Mechanism removal: the `competing_senses` removal also takes out the bare-name
and qualifier rules, and T4 and T7 fail under it; T3 and T5 fail without the
resolver. The negative controls C6, T2 and T9 still pass and still fail with
the naming gate removed.

### Round 2 on corpus v4 (2026-09-28, history): red

Corpus v4 (design amendment A1) registers the corpus vault's own observation
categories and routes them to roles through the schema writers, and adds 110
ordinary Notes pages; both trees index 147 pages, above the retrieval carry's
floor of 100. The fixtures and thresholds are unchanged. Every amended score
(A2 unit-to-parent recall, A4 hedged poison) is reported beside the raw one.
A4 removes a hedged poison hit and nothing else: it never marks a case as
hedging, so it cannot waive a status mismatch (integrity review F1).

**Known gap in the raw scorer (disclosed, not fixed).** Raw precision counts
resolved anchors, units and pointers, never ambiguity candidates, and a
listed candidate turns the status into `ambiguous`. So a C7 packet holding
its gold hubs as `partial` anchors, with the gold facts in the rendered
ambiguity, passes with a wrong page added as an ambiguity candidate. The same
packet without that candidate fails on status. This predates round 2, and the
raw scorer stays frozen. Amendment A7 (opt-in) puts every ambiguity candidate
of a positive case into the precision denominator, like a served anchor. It
is reported in its own column. On v4 the real C7 packet lists only its two
gold hubs, so A7 changes no verdict.

**Amendment A8 (opt-in): a carried gold page counts.** When a positive case's
gold page is served `retrieval_carried` together with at least one of its own
units, the expected `resolved` status is read as satisfied. Every `#unit-`
fragment of a bound gold page is credited to that page in precision as well as
recall; A2 amended recall only. Units of any other page stay distinct. The
reason: the product labels carried context honestly, and what matters to the
user is whether the agent receives the right page and fact. A8 is reported in
its own column. On v4 it moves only C4: status and precision are satisfied,
but C4 stays red on recall (0.50), because the colleague's entity is never
reached.

**Amendment A9 (opt-in): agent-choice scoring.** The agent reasons and
chooses; the compiler's job is recall within budget with truthful labels. A
positive case passes when every gold page arrives under an honest label (a
resolved, partial or `retrieval_carried` anchor, or an ambiguity candidate)
with the case's gold facts served, and nothing is mislabelled: no non-gold
page is served as resolved or as current state, and no poison as resolved.
Extra honestly labelled siblings do not fail a case within the packet budget.
Twins and C6 are scored unchanged. The digest (`bfedee2f…`) was pinned before
the first run. On v4 A9 gives 5/18 on the base runtime and 9/18 on the
activation-quality runtime (equal to its raw score, 9/18): each red positive
still misses a gold page, and none fails for a mislabel alone. C4 fails only because its
colleague's entity never arrives.

**Pinned reds by ruling.**
- **C2 and C9 (R3).** The grill resolves only partially. A shared tag is not
  corroboration on a real vault, and semantic corroboration is future
  sensed-model work.
- **C3 (R4).** Besides the Planning identity (A3), "the next roadmap item"
  relies on workspace context a cold run lacks. The keyed variant belongs in
  the continuity group; it waits on the corpus v5 ruling.

The ordinary notes are realistic, topic-diverse pages, and several share words
with the fixture turns on purpose. One is a deliberately hard lexical
distractor: "Oven temperature conversions" was written with T6's turn in view,
and carries its distinctive words (temperature, Fahrenheit, Celsius). It makes
T6 harder, not easier, and it is kept by ruling (F5): a vault in real use has
distractors like it.

| Case | Raw | A2+A4 | A7 | A8 | A9 | Why it fails today |
|------|-----|-------|----|----|----|--------------------|
| C1 | red | red | red | red | red | Resolves the subscriptions collection; the two gold notes never arrive (recall 1/3) |
| T1 | red | red | red | red | red | The carry reaches the step-count fitness-goal page, a fixture page bound as C1's poison, outside T1's empty gold (see the corpus v5 ruling request) |
| C2, C9 | red | red | red | red | red | The grill stays `partial`; the turn abstains (recall 1/2). Pinned by ruling R3 |
| T2 | pass | pass | pass | pass | pass | |
| C3 | red | red | red | red | red | Product red (A3, close-memory-loop 6.12): the plan anchor is the collection, not the item. Also relies on workspace context a cold run lacks (R4) |
| T3 | red | red | red | red | red | Product red (A3): resolves the other workstream's collection |
| C4 | red | red | red | red | red | The carry brings the failure note; nothing reaches the colleague's entity (under A8 only recall 0.50 remains) |
| T4 | red | red | red | red | red | Product red (A5): a bare first name stays `partial`; the carry also reaches C4's failure note |
| C5 | pass | pass | pass | pass | pass | |
| T5 | red | red | red | red | red | The scanner cart's own resource unit is served; D9 counts the fragment as foreign, and A2 amends recall only |
| C6 | pass | pass | pass | pass | pass | |
| T6 | red | red | red | red | red | The carry reaches an ordinary note on oven temperature conversions |
| C7 | pass | pass | pass | pass | pass | |
| T7 | red | red | red | red | red | Product red (A5): the scoped turn also resolves the market hub |
| C8, T8 | red | red | red | red | red | The gold notes are not anchors, and no word of the turn carries them |
| T9 | pass | pass | pass | pass | pass | |

The table above is the base runtime's. On the activation-quality runtime (recorded
after merging the base at A9 and A10, and after the run-punctuation, casing and
headline rules) raw, A2+A4, A7, A8 and A9 are each 9/18: T3, T4, T5 and T7 pass as
well as the five above. Before the merge, raw and A2+A4 were 9/18 and A9 was 5/18 on the
base's runtime; the raw and amended scores did not move with these rules. Base runtime: raw 5/18, A2+A4 5/18, A7 5/18, A8 5/18, A9 5/18. No amendment changes a verdict on v4: no gold
note reaches a packet only as a unit, no poison is served as a hedge, and no
positive case lists an ambiguity candidate outside its gold.

Mechanism removal on v4. The kill switch fails every positive case. C5 fails
without governed current state and C7 without competing-sense abstention.
The negative controls C6, T1, T2 and T9 are now load-bearing: each fails with
the naming gate removed, where on v3 each passed under every removal. Under
the kill switch they pass, as a negative control must, because a disabled
compiler abstains.

### The keyed continuity group (amendment A6)

`membench.utility.context_activation_continuity` pre-registers four keyed
fresh-session cases, each from a `memory-loop` spec scenario, with gold for the
referent and for the `recent_context` block. The earlier session's acts go
through supported doors (an `anchor` pick, an `episode_memory` record, an
`edit_memory` commit), then one fresh `activate_context` call is made.

The current group is v3 (its digest is the `CONTINUITY_SHA256` pin in
`tests/test_context_activation_continuity.py`, pinned before its first run and
re-pinned when corpus v5 relabelled the corpus). It keeps v1's cases and gold. Everything a packet serves must
belong to a referent page: anchors of every status, ambiguity candidates,
units, pointers and current-state entries. A unit or state entry of the
referent page is fine; one of any other page fails. The keyless K3 must serve
nothing at all. Refs are compared on canonical page identity, read back from
the vault before the fresh turn: a memory ref, a path, a path without the
knowledge-base prefix, and a `#fragment` of any of them all name the same
page. So a hot-page ambiguity that lists a page by path matches its anchor.
The digest also covers a sha256 of the scorer module's source. The result is
in `docs/benchmarks/context-activation-continuity-2026-09-v3.json`.

History, kept as recorded:
- v1 (`de5e7900…`, 4/4, `context-activation-continuity-2026-09.json`) did not
  check extra served pages.
- v2 (`b208a986…`, 4/4, `context-activation-continuity-2026-09-v2.json`)
  checked anchors and ambiguity only, on one exact ref spelling. Its scorer
  passed packets carrying a unit, pointer or state entry of a wrong page,
  which v3 fails.

| Case | Scenario | Result |
|------|----------|--------|
| K1 | "continue" from a fresh session in the earlier session's workspace, after newer work in another workspace | v3 pass (v2, v1 pass): serves only the bench, its units and its state |
| K2 | "where were we" after a recorded episode | v3 pass (v2, v1 pass): serves only the entity and its unit |
| K3 | a keyless bare "continue" | v3 pass (v2, v1 pass): abstains, serves nothing, still lists the page |
| K4 | "continue" after an edit of an ordinary, non-anchor note | v3 pass (v2, v1 pass): carried on recency, only its own units |

The group is not one of the eighteen and does not enter their verdict.

### Round 1 on corpus v3 (2026-09-28, history): red

Seven of eighteen fixtures pass: C5, T5, C6, C7, T1, T2 and T9. The audit is
red, and C9's padding comparison fails because the padded packet surfaces no
resolved reference to measure precision on.

| Case | Why it fails today |
|------|--------------------|
| C1 | Resolves the subscriptions collection; the two gold notes never arrive (recall 1/3) |
| C2, C9 | The grill stays `partial` on retrieval alone; the turn abstains |
| C3 | Abstains; the plan anchor it could reach is the collection, not the gold item |
| T3 | Resolves the other workstream's collection, which is outside its gold item |
| C4 | Nothing in the turn reaches the colleague's entity |
| T4 | The two same-first-name people stay `partial`; the turn abstains `unresolved`, not `ambiguous` |
| T6 | The grill is listed as a `partial` anchor, and the scorer counts it as poison |
| T7 | The scoped turn still resolves the market hub beside the feature hub |
| C8, T8 | The gold notes are not anchors, and nothing carries them |

### Mechanism removal on v3 (history)

`FIXTURE_MECHANISMS` pre-registers the mechanism each fixture measures, and
`removed()` takes it out of the running compiler. With
`EXOMEM_DISABLE_WORKING_SET=1` every positive case fails. C5 fails without
governed current state, T5 without anchor resolution and C7 without
competing-sense abstention. C6, T1, T2 and T9 pass under every removal,
including the kill switch: nothing in this corpus serves their words, so on
this corpus they cannot catch a false activation. The test names them rather
than skipping them.

### Read this before treating the v3 report as compiler quality (task 1.8)

- **Gold notes have no anchor, and the carry refuses the corpus.** Insight,
  pattern, failure and design notes are not activation anchors. They reach a
  packet only through the retrieval carry, which refuses a corpus below 100
  indexed pages. Both trees index 37: the padded tree's 200 distractors are
  Evidence, which the lexical catalogue does not count.
- **Most gold notes use unregistered observation categories.** `operating
  constraint`, `pattern`, `method`, `failure` and `current state` resolve as
  unregistered, and role lanes select units by registered category. Even a
  forced carry of C8's gold page serves nothing. Only C3's `design` note yields
  a unit.
- **A unit of a gold note does not recall the note.** Units carry fragment refs
  (`<memory ref>#unit-…`). Under D9 a fragment stays a distinct reference, so it
  counts against precision and earns no recall.
- **Planning identity.** The plan anchor is the collection manifest; C3 and T3
  gold the item page.
- **Recent context is turn-independent.** Every packet carries the same
  recent-context block on a tree, which here lists the open Planning items. The
  scorer counts turn-derived channels only, so C6's zero-injection bound is
  measured over those.
- **The cases run cold.** No continuity token and an empty hot profile, so
  follow-up carry and recency referents are not measured by these eighteen
  cases.

## Producing a dry-run argv for one arm (never executes anything)

```python
from pathlib import Path
from epistemic.journeys.f27_replay import discover_agent_envelope
from membench.utility.context_activation_arms import (
    context_activation_turn_argv, dry_run_lines, generate_context_activation_episode, variant_for_case,
)

envelope = discover_agent_envelope()
episode = generate_context_activation_episode(seed=1, variant=variant_for_case("C1"))
plan = context_activation_turn_argv(episode=episode, arm_id="A4_nudged_recall", envelope=envelope, out_dir=Path("/tmp/context-activation-dry-run"))
print("\n".join(dry_run_lines(plan)))
```

This prints the exact argv and environment delta and writes nothing --
`plan.workdir` and `plan.system_prompt_file` are computed paths, not created
files. Running the real `claude -p` session this argv describes is a
separate, explicitly authorized step.

## Building the private-vault snapshot (local only, never committed)

```
uv run python scripts/private_vault_snapshot.py --source /path/to/vault --dest /path/outside/every/repository
```

Refuses outright if `--dest` resolves inside any git checkout. Excludes any
page whose body contains a fixture turn verbatim and lists the exclusion
(by path only) in `SNAPSHOT_MANIFEST.json`. The corpus's own logical
gold/poison keys (`epistemic.corpora.context_activation.KEY_KINDS`) still
need a locally-authored, never-committed `key -> real path` mapping before
`membench.utility.context_activation.score_case` can resolve a real packet
against them -- `score_case`'s `key_to_ref` parameter exists for exactly
this, and defaults to the identity mapping for a hand-written oracle packet
that already speaks in the fixture's own keys.

## Naive-path latency and deterministic baseline reports

Both are model-free measurements this change actually runs (not dry-run
placeholders): see the run reports and the measured latency constant
recorded in `epistemic.corpora.context_activation.MEASURED_LATENCY_MS`.
Evidence from a run against a real vault is preserved in the owner's
knowledge base, never in this repository.
