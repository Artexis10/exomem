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

A measured spike on a disposable copy of a real vault (about 13,600 pages and 11,000 plain links) compared small local models with a frontier ceiling on the same cases. The results are under Measured. In short:

- Off-the-shelf small instruct models choose a fitting relation far less often than the frontier model, and their confidence does not separate right picks from wrong ones.
- A small model distilled on frontier picks improves sharply, and its confidence does separate. It runs on one CPU thread in a Cloud cell's memory.

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

- **Runtime.** The instrument drives a narrow runtime interface: tokenize a rendered prompt, evaluate it, and return the last position's logits. This change ships that interface and no concrete runtime. The first pin brings its runtime and dependency, under the rule that weights load file-backed and read-only, so all workers on a node share one copy (`ship-models-as-shared-onnx`, D1). Private memory is then the context cache and compute buffers. The context window is fixed per question at the smallest power of two that holds its longest admitted input; for `relation.type` that is 2,048 tokens.
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
- **Order.** Pages whose graph `source_hash` changed since the producer last saw them come first. A persisted cursor then walks the remaining pages in path order, so every page is reached once per pass.
- **Bounds.** At most `PAGES_PER_TICK` source pages and `LINKS_PER_PAGE` links per page per tick. These are constants of the producer, not settings.
- **Submission.** One replaceable scope per source page, with one owned request per link. The stable request key is the source path, the target path and the link's occurrence anchor. A changed page replaces its scope atomically, so stale requests are removed.
- **Ingestion.** The tick follows the ledger and writes one projection row per current link: request key, reading id, verdict, top key and probability, and the menu digest. A row whose inputs no longer match the current page is dropped.

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

The spike ran on a disposable copy of a real vault, on a shared 16-core desktop. Nothing from it is in the repository.

**Sets.** Two cohorts, each split by source page into a dev set and a held-out set:

- *Plain links* (production-shaped): 100 dev and 100 held-out body wikilinks and plain-link bullets, one per source page.
- *Authored relations*: 100 dev and 120 held-out authored specific relations, shown with the relation statements removed.

The training set holds 5,021 links from 2,066 other source pages. No page that appears in a dev or held-out case, as source or target, appears in training.

**Reference and judge.** The frontier ceiling is Claude Opus, given the same prompt through a JSON schema whose answer is an enum of the case's letters. Every specific proposal from every arm is judged blind: "does SOURCE relation TARGET hold?" On dev the judge is Opus. That judge is the students' teacher's family, so dev numbers are calibration. The held-out judge is a model from another family.

**Students.** Qwen3 0.6B and Qwen3 1.7B, each fine-tuned with LoRA (rank 32, all linear layers) on the Opus pick for each training link. The loss is cross-entropy over the case's letters at the answer position, with half of the menus shuffled. Each student runs as a Q4_K_M GGUF through llama.cpp on one CPU thread.

**Dev results, plain links** (judged precision, with yield = judged-true proposals per case):

| Arm | Threshold | Precision | Wilson 95% | Yield |
|---|---|---|---|---|
| Opus (ceiling, self-judged) | none | 0.84 (49/58) | 0.73–0.92 | 0.49 |
| Haiku | none | 0.52 (36/69) | 0.41–0.64 | 0.36 |
| Qwen3 4B Instruct, not tuned | none | 0.33 (27/83) | 0.23–0.43 | 0.27 |
| Qwen3 1.7B, not tuned | none | 0.24 (12/50) | 0.14–0.37 | 0.12 |
| 0.6B student, 2,300 labels | none | 0.46 (19/41) | 0.32–0.61 | 0.19 |
| 0.6B student, 5,021 labels | p ≥ 0.6 | 0.63 (22/35) | 0.46–0.77 | 0.22 |
| 1.7B student, 5,021 labels | p ≥ 0.4 | 0.75 (21/28) | 0.57–0.87 | 0.21 |

On the authored cohort at the same thresholds, the 1.7B student reaches 0.83 (25/30, Wilson 0.66–0.93, yield 0.25) and the 0.6B student 0.76 (28/37, Wilson 0.60–0.87, yield 0.28). Opus reaches 0.76 (56/74) there without a threshold.

Confidence thresholds did not help the untuned models: at every cut, Qwen3 4B stayed between 0.24 and 0.31 on the authored set. They do help the students. Doubling the training labels raised the 0.6B student's precision at equal yield from 0.46 to 0.63. Each student's threshold was fixed on dev before the held-out run.

**Held-out results** (one run, thresholds as fixed on dev; judged by Opus, a secondary judge from the teacher's family; the cross-family judgement is pending):

| Set | Arm | Precision | Wilson 95% | Yield | Paired yield minus Opus, 95% CI |
|---|---|---|---|---|---|
| Plain links | Opus | 0.70 (45/64) | 0.58–0.80 | 0.45 | |
| Plain links | 0.6B student, p ≥ 0.6 | 0.53 (16/30) | 0.36–0.70 | 0.16 | −0.29 [−0.38, −0.20] |
| Plain links | 1.7B student, p ≥ 0.4 | 0.71 (17/24) | 0.51–0.85 | 0.17 | −0.28 [−0.36, −0.19] |
| Authored | Opus | 0.84 (78/93) | 0.75–0.90 | 0.65 | |
| Authored | 0.6B student, p ≥ 0.6 | 0.78 (25/32) | 0.61–0.89 | 0.21 | −0.44 [−0.53, −0.35] |
| Authored | 1.7B student, p ≥ 0.4 | 0.83 (19/23) | 0.63–0.93 | 0.16 | −0.49 [−0.58, −0.40] |

No arm meets the admission bar (D6) on held-out plain links, Opus included. The 1.7B student matches Opus's precision there at under half of its yield.

**Cost.** Each link is one evaluation of a prompt of about 900 tokens (longest 1,447), so a 2,048-token context holds every case. Measured on one thread at that context, on a shared host whose load average ran from 14 to 34 on 16 cores, so the CPU times are upper bounds:

| Model | CPU s per link (median) | Private memory | File-backed weights |
|---|---|---|---|
| 0.6B Q4_K_M | 13–16 | 584 MiB | 391 MiB |
| 1.7B Q4_K_M | 31–38 | 1,017 MiB | 1,064 MiB |

A Cloud cell has 2 CPUs and 3 GiB, and a busy owner cell's working set is about 1.6 GiB. The 0.6B student fits beside it with room to spare. The 1.7B student leaves about 0.4 GiB. Under the worker's 300 CPU-seconds per hour, the 0.6B student types about 20 links per hour. The first pass over 11,000 plain links then takes about three weeks, and new links keep up.

## Risks and trade-offs

- **The bar may not be met on CPU.** Then `relation.type` stays refused and the plumbing ships dark. The queue's deterministic behaviour is unchanged.
- **The menu shapes the answer.** A relation missing from the menu cannot be proposed. The menu is the same one the agent sees, so the sensor never widens the agent's choice.
- **Backlog time.** At one thread under the hourly budget, the first pass over a large vault takes weeks. New links are few per day, so the steady state keeps up. The Dreamer's changed-first order keeps recent work current.
