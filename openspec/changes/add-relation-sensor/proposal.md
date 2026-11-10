## Why

Most links in a working vault say nothing about how two notes relate. A real vault of about 13,600 pages holds about 11,000 plain links on 2,750 pages: body wikilinks, plain-link bullets and the remaining generic bullets. Writers refuse a new generic relation (since 0.167.0), but that refusal only covers new edges, and only at the moment an agent writes. Nothing helps the agent type the plain links that already exist, or the body wikilinks it writes later.

`add-sensed-epistemic-model` built the pieces a background reader needs: a closed-question registry, an append-only readings ledger, a shared sense queue, and a supervised worker child with its own budgets. Its instruct-model slice stalled on model size: the candidates that answered well needed 15 GB, which no Exomem Cloud cell can carry. The owner's direction is one small model that a Cloud cell can run, reused by many narrow sensors and scheduled by the Dreamer. Relation typing is the first sensor.

## What Changes

- Add **the sensor model**: one pinned small instruct model that answers every multiple-choice question. It runs on CPU, with one thread, inside the existing sensor worker child. One evaluation reads the probabilities of the option letters at the answer position. It never samples or stores text. The pin, its runtime and each question's template and letter map are repository data. Missing weights refuse only the generative questions; the admitted NLI instrument keeps working.
- Add the **`relation.type` question**. Its inputs are a source page excerpt, a target page excerpt, the line that carries the link, and a menu of specific relations. The menu is the admissible relations for the ordered pair, ranked by `relation_options.ranked_options` with the vault's own usage, at most 12, plus a final "none of these". The verdict names one menu relation, `none`, or `abstain`. The reading identity binds the menu, so a changed menu never reuses an old answer. It replaces the planned one-relation-at-a-time `relation.applies` question, which would cost up to 12 evaluations per link.
- **Schedule it from the Dreamer.** Each sensed tick reads a bounded number of source pages from the graph, changed pages first and then a persisted cursor through the rest. It submits one replaceable scope of `relation.type` requests per page to the shared queue. Requests come from published graph rows only. A tick never parses Markdown and never loads a model.
- **Deliver through the relation review queue.** A plain link with a current confident reading carries a proposal: the sensed relation, its probability and the reading id. The menu stays the queue's options. A body wikilink accepts as a new typed bullet. A plain-link or generic bullet accepts by replacing that exact bullet. Every accept stays the agent's governed decision through `connect_memory(operation="accept-relation")` and cites the reading. The queue read stays graph-native: it reads the projection and runs no model.
- **Admit by measurement.** A sensor model is admitted for `relation.type` only with recorded evidence: blind judged precision of at least 0.80 (Wilson lower bound at least 0.70) at a yield of at least 20%, plus CPU time and memory per case. A pin is trained only on data without private vault content.
- Re-lane the relation-typing part of `add-sensed-epistemic-model` task 9.4 to this change, and replace its `relation.applies` input contract. Its task 9.1 keeps `mention.same_referent` and reuses the sensor model from this change.

## Capabilities

### New Capabilities

- `relation-sensor`: the shared sensor model contract, the `relation.type` question, its Dreamer scheduling over the plain-link cohort, its admission evidence, and its delivery as governed relation-queue proposals.

### Modified Capabilities

None. The instrument rules in `frozen-verifiers` and the queue and worker contracts of `add-sensed-epistemic-model` already admit a closed-question generative instrument. This change adds a question with its producer and consumer under them, and amends that active change's design in place.

## Impact

- **Code:** new `sensing_llm` (the sensor model instrument) and `relation_sensor` (request building, the Dreamer producer and the queue annotation); `sensing` (the `relation.type` registry entry and its label map); `sensor_worker` (admission dispatch for generative questions); `sensed_model` (the producer call inside the sensed tick and one projection table); `relation_queue` and the graph-native batch (sensed proposals and the bullet-replacing accept). The concrete model runtime and its dependency arrive with the first pin, in its own change.
- **State:** one new table in the disposable `sensing/projection.sqlite`. The ledger schema is unchanged; readings use the existing version-2 payload.
- **Runtime:** default-off, behind the existing sensing setting. With sensing off, or with no admitted sensor model, every read is byte-identical to a build without this change. The worker's existing hourly CPU and judgement budgets bound the cost.
- **Pure substrate:** the sensor measures and proposes. Only the agent's governed accept writes a relation.
