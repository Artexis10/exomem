# Design: the relation sensor

## Context

`add-sensed-epistemic-model` built the substrate a background reader needs:

- a repository question registry (`sensing.QUESTIONS`) with versioned templates and label maps;
- version-2 requests with named input slots (`sensing.InputSlot`, `sensing.SenseRequest`);
- an append-only readings ledger (`sensing_ledger`);
- one shared `sense_jobs` queue in the disposable projection, with owned, replaceable scopes (`sensed_model.submit_scope`);
- a supervised, disposable worker child (`sensor_worker`) with CPU, judgement and memory budgets and per-question admission.

Its design (D1) names a generative instrument for later slices, and a `relation.applies` question that checks one proposed relation at a time. Nothing answers a generative question yet.

The relation review queue (`relation_queue`) already lists plain links as candidates: body wikilinks, plain-link bullets and generic bullets. It offers the admissible registry relations for each ordered pair (`relation_options.ranked_options`), refuses a new generic edge, and writes only through the agent's governed accept. It does not say which relation fits.

Historical experiment reports describe small-model relation typing and distillation on disposable vault copies.
The complete cases, outputs and measurement receipts are unavailable; the reports do not qualify a model for admission.
This change delivers the disabled pipeline with an empty pin registry and no concrete model runtime.
The Measured section distinguishes reported observations from reproducible evidence.

## Goals and non-goals

Goals:

- One sensor model, pinned and CPU-only, that answers closed multiple-choice questions for several sensors.
- A `relation.type` question that names a specific registry relation for a plain link, or `none`.
- Dreamer scheduling that is bounded per tick and pages through a large vault.
- Delivery as relation-queue proposals that only the agent accepts.

Non-goals:

- No model writes, rewrites or summarises vault content.
- No new vocabulary in code. Relations come from the registry and the vault overlay.
- No API placement in this change. R7 of `add-sensed-epistemic-model` still allows it later, opt-in.
- No GPU placement (R5 of `add-sensed-epistemic-model`).

## Decisions

### D1. One sensor model answers multiple-choice questions

The sensor model is one pinned small instruct model. Its pin is a repository artifact under the frozen-verifier rules: model, revision, artifact manifest and sha256, runtime and runtime version.

A generative question's template renders a chat prompt. Vault text goes only inside delimited data slots, with `&`, `<` and `>` escaped. The options are listed with letters, and the prompt ends at the answer position. The instrument evaluates the prompt once and reads the logits of the option letters at that position. It normalises them over the case's letters only. It never samples, and it never stores generated text.

The instrument identity also binds the template version and the label-token map: the token id of each letter in the pinned tokenizer.

- **Runtime.** The instrument drives a narrow runtime interface: tokenize a rendered prompt, evaluate it, and return the last position's logits. This change ships that interface and no concrete runtime. The first pin brings its runtime and dependency, under the rule that weights load file-backed and read-only, so all workers on a node share one copy (`ship-models-as-shared-onnx`, D1). Private memory is then the context cache and compute buffers. The `relation.type` context limit is 2,048 tokens. A future pin must prove its admitted input sizes; longer inputs abstain.
- **One thread, one case.** The worker evaluates one case at a time on one thread, as the NLI instrument does. Its existing hourly CPU-seconds and judgement budgets bound the cost.
- **Shared prefix.** Requests from one source page share the prompt prefix up to the end of the source excerpt. The instrument may keep that prefix's cache across consecutive requests from one page. Each request still produces the same vector as a cold evaluation, within rounding.

### D2. The `relation.type` question

`relation.type` asks: which listed relation does the source note's author mean by this link?

Its input is version 2, with four named slots in this order:

| Slot | Extractor | Text |
|---|---|---|
| `source` | `page-excerpt-v1` | The source page's title, then its body with relation statements removed and wikilinks rendered as their display text, cut at 900 characters |
| `target` | `page-excerpt-v1` | The same for the target page |
| `link` | `link-sentence-v1` | The line that carries the link, cut at 300 characters, or empty for a bare bullet |
| `menu` | `relation-menu-v1` | The ordered options: each canonical key with its registry label and definition |

The menu is the ordered pair's admissible relations exactly as the relation review queue offers them (`RelationFit.options`, ranked by `relation_options.ranked_options`, at most 12), then a final "none of these". The producer calls that same function, so a proposal never names a relation that the accept would refuse. The `menu` slot hashes exactly these entries. An edit to an unrelated registry entry changes no reading identity; an edit to a listed definition does.

The output vector is `answer`. Its columns are the menu's keys in letter order, then `none`.

Label map `relation-type-v1`:

1. `abstain` with reason `input_too_long` when the rendered prompt exceeds the question's context window. The instrument does not truncate.
2. The top key itself, as the verdict label, when the top column is a key and its probability is at least `propose_min`. This is a *relation verdict*.
3. `none` when the top column is `none`.
4. `abstain` with reason `low_confidence` otherwise.

The closed label set of a reading is therefore its own menu plus `none` and `abstain`. Code treats only `none` and `abstain` specially; it never names a relation key.

`propose_min` is part of the label map, and the admission evidence (D6) fixes it. A changed threshold is a new label-map version, and it re-derives verdicts from the stored vectors without sensing again.

This replaces `relation.applies` in `add-sensed-epistemic-model` D1. One evaluation per link answers the whole menu. A yes-or-no check per option costs one evaluation per option, up to 12 per link, which the CPU budget cannot carry at vault scale. The agent still chooses: the sensor proposes one menu entry and the agent accepts, picks another option, or declines.

### D3. The Dreamer schedules it

The sensed tick gains a bounded `relation.type` producer. It never loads a model. Candidates, anchors, page types and menus come from published graph rows. The graph does not publish page bodies, so the producer reads the text of the chosen source pages and their link targets from the page files. It uses a file only when the file's content hash equals the graph's `source_hash` for that page, so line anchors and text describe the same bytes. A mismatched page is skipped and is picked up again once the graph catches up. The relation queue read never reads page files.

- **Candidates.** The producer reads two kinds of plain link from the graph:
  - body wikilinks, from the same indexed wikilink evidence the relation queue's `wikilink` method reads;
  - authored relation bullets whose relation is the registry's plain-link key or a generic relation.

  A pair that already has a specific authored relation is skipped.
- **Order.** The tick submits changed source pages first, up to its fixed bound. A persisted cursor walks the other pages in path order. Changes beyond the tick's bound remain reachable through that cursor.
- **Bounds.** At most `PAGES_PER_TICK` source pages and `LINKS_PER_PAGE` links per page per tick. These are constants of the producer, not settings. Cursor queries use the existing primary file keys. Excerpts parse only selected page bodies through the maintained Markdown grammar. Exact retype lookups use a shared graph index over source path, origin, source anchor and target page; that index adds per-edge storage and maintenance cost.
- **Submission.** One replaceable scope per source page, with one owned request per link. The stable request key is the source path, the target path and the link's occurrence anchor. A changed page replaces its scope atomically, so stale requests are removed.
- **Ingestion.** The tick batches ledger lookups for a bounded page of pending rows. Unchanged inputs retain their verdicts when their scope is replaced. Each row binds both published page hashes and the menu digest. Queue reads require matching graph hashes and an instrument with current admission evidence. A changed menu suppresses the prior proposal. A replaced ledger clears cached verdicts before bounded refill.

### D4. Delivery through the relation review queue

The queue read stays graph-native (`epistemic-graph`, "Relation review queue assembly is graph-native and bounded"). It reads the projection rows and runs no model.

- **A proposal on a wikilink candidate.** A `wikilink` candidate whose current reading is a relation verdict carries a `sensed` block: the relation key, its probability, the reading id and the instrument id. Its options stay the menu the queue already offers. The sensed relation is marked as proposed, never as chosen.
- **A new retype candidate.** Today an authored plain-link or generic bullet is never a queue candidate. It becomes one, with the new method `retype`, only while its current reading is a relation verdict. Its evidence names the exact bullet line. Without such a reading the bullet stays out of the queue, as today.
- **No proposal.** A `none` or `abstain` verdict adds nothing. The candidate stays as it is today.
- **Accept.**
  - For a body wikilink, the accept adds a typed bullet.
  - For a `retype` candidate, the accept replaces that exact bullet line with one typed bullet to the same target, bound by the page hash it was read against. It uses the governed edit's exact-replacement form, not the section append the other methods use.
  - A `retype` candidate skips the authored-or-settled check that the other methods run, because the bullet it replaces is itself the authored edge. Its freshness is the exact bullet line plus the page hash.
  - While a candidate carries a proposal, the accept offers the generic relation's menu for it, so the agent may name the proposal or any other menu relation without a separate vocabulary decision. Without a proposal, a plain link offers what it offers today.
  - The accept is the agent's `connect_memory(operation="accept-relation")`. It never adopts the proposal on its own: the agent names the relation. The audit reason cites the reading id. The governed writer validates the relation as it does today.
- **Ordering.** Candidates with a proposal may sort ahead of others within a source group. The group order and the queue's bounds do not change.

### D5. Absence is silence

With sensing off, with no admitted sensor model, or with no reading for a link, every queue read and every Dreamer output is byte-identical to a build without this change. A refusal of `relation.type` is scoped to that question: the NLI instrument keeps running.

### D6. Admission is measured, not asserted

A sensor model is admitted for `relation.type` only with recorded evidence on a frozen evaluation set:

- The set holds two cohorts of real plain links and authored relations, with every page disjoint from any training data.
- A strong model from another family judges each proposal blind: does "SOURCE relation TARGET" hold? The judge sees no arm names.
- The bar: judged precision of at least 0.80 with a Wilson lower bound of at least 0.70, at a yield of at least 20% of the cases. The evidence fixes `propose_min`.
- The evidence records CPU seconds per case on one thread, and private and file-backed resident memory.

The evidence is a repository artifact with the pin. Vault text from the evaluation set never enters the repository: the evidence holds counts, intervals and digests only.

### D7. A pin is trained only on shareable data

A pinned sensor model ships with the product, so its weights must not encode any private vault. A model fine-tuned on a personal vault never becomes a pin. The private spike under Measured shows what distillation can reach; a shippable pin needs its own training set without private content and its own admission run.

## Measured

### Evidence status

Historical reports describe one held-out run for private-trained students and another for synthetic-trained students.
The complete cases, arm outputs, raw judgments, timing rows, model artifacts and original model E2E receipts are unavailable.
Preserved scripts and aggregate reports cannot reconstruct those artifacts or verify the recorded paired confidence intervals.
The cross-family judgment therefore remains unrun, and no model is admitted.

The reports describe 100 plain-link cases and 120 authored-relation cases for the private-trained students.
They describe a different cohort of 150 plain-link cases and 100 authored-relation cases for the synthetic-trained students.
These cohorts do not support a paired comparison between private and synthetic training.
The original thresholds remain frozen; these used sets are not fresh held-out data for another experiment.

### Historical aggregates, not admission evidence

The following counts come from preserved reports, not a fresh read of raw judgments.
The judge shared the teacher's model family, so these are secondary calibration results.
Precision means judged-true proposals divided by proposals; true yield means judged-true proposals divided by all cases.
Proposal coverage is a separate measure: proposals divided by all cases.

| Reported plain-link cohort | Arm | Frozen threshold | Reported true / proposals | Reported true yield |
|---|---|---|---|---|
| Private-trained held-out, 100 cases | Frontier ceiling | none | 45 / 64 | 45% |
| Private-trained held-out, 100 cases | 0.6B student | p >= 0.6 | 16 / 30 | 16% |
| Private-trained held-out, 100 cases | 1.7B student | p >= 0.4 | 17 / 24 | 17% |
| Synthetic-trained held-out, 150 cases | Frontier ceiling | none | 62 / 82 | 41% |
| Synthetic-trained held-out, 150 cases | 0.6B student | p >= 0.4 | 15 / 30 | 10% |
| Synthetic-trained held-out, 150 cases | 1.7B student | p >= 0.0 | 12 / 26 | 8% |

None of these reported student results meets D6's true-yield floor.
The reports do not establish equivalence to the frontier ceiling or a causal benefit from synthetic training or label batching.
The old scorer could omit missing judgments or arm rows; these aggregates cannot resolve that limitation.
A resumed evaluation must account for every frozen case and every missing output before computing a score.

### Resource claims remain unverified

Reports list process CPU times of 13–16 seconds per link for the 0.6B student and 31–38 seconds for the 1.7B student.
Host contention does not make process CPU time a guaranteed upper bound.
The memory fields came from resident anonymous and file-backed pages; they do not establish private allocation or shared weight cost.
The raw timing rows and model artifacts are unavailable, so these reports prove neither Cloud-cell headroom nor backlog completion time.
A future pin needs a measured CPU and memory result under the deployed limits, including the cell's other resident work.

### Pipeline acceptance and remaining model work

Deterministic integration checks exercise Dreamer submission, worker readings, queue proposals and governed acceptance on disposable synthetic state.
They verify pipeline behavior with a controlled instrument, not learned-model accuracy.
The historical real-model E2E pass remains reported, with its original receipts unavailable.

`tests/test_relation_sensor_integration.py::test_producer_selection_work_stays_bounded_at_50000_graph_nodes` exercises the real producer against the maintained graph schema.
The final scoped run records 34 graph queries at both corpus sizes and approximately 1,700 versus 1,500 SQLite VM steps, sampled every 100 steps.
Its JUnit properties record 85 cursor VM steps and primary-key query plans for cursor and source lookups.
The high-degree retype check records 98 VM steps with the exact-anchor index and 6,153 without it, returning identical candidates.
These checks measure deterministic query work, not model quality or end-to-end request latency.

The full usable-model objective remains open under `add-sensed-epistemic-model` task 9.1.
It includes exact artifact recovery, or a separately specified fresh experiment if recovery fails, and independent cross-family judgment.
Any future pin also needs shareable training data, applicable provider terms and its own admission evidence.
This change enables no model, sends no vault text to a provider, and changes no production sensing setting.

## Risks and trade-offs

- **The bar may not be met on CPU.** Then `relation.type` stays refused and the plumbing ships dark. The queue's deterministic behaviour is unchanged.
- **The menu shapes the answer.** A relation missing from the menu cannot be proposed. The menu is the same one the agent sees, so the sensor never widens the agent's choice.
- **Backlog time.** No admitted runtime establishes initial or steady-state throughput. The first pin must measure both at the product's load target. Changed-first ordering prioritizes source edits. A target edit hides a stale proposal until the source is revisited.
