## 1. The sensor model instrument

- [x] 1.1 Add the `relation.type` entry to `sensing.QUESTIONS`: slots `source`, `target`, `link`, `menu`; extractors `page-excerpt-v1`, `link-sentence-v1`, `relation-menu-v1`; vector `answer`. Add label map `relation-type-v1` with `propose_min` and the abstain reasons `input_too_long` and `low_confidence`. Verify: unit tests for each label-map rule in order, column and width validation, and re-derivation after a threshold change.
- [x] 1.2 Add `sensing_llm`: the sensor-model pin registry entry shape, manifest digest check, file-backed read-only load, template rendering with escaped data slots, one evaluation per case, letter logits normalised over the case's letters, and `input_too_long` without truncation. The runtime is an optional extra. Verify: tests with a stub runtime for escaping, the letter-only vector, refusal on a digest mismatch, and refusal of an environment-named model.
- [x] 1.3 Dispatch generative questions in `sensor_worker`: question-scoped admission and refusal, evidence-gated admission (task 4.1), one thread and the existing budgets. Verify: missing sensor-model weights refuse `relation.type` while `pair.relation` keeps running.

## 2. The Dreamer producer

- [x] 2.1 Add `relation_sensor` request building from published graph rows: the plain-link cohort the relation queue lists, the excerpt and link-line extractors, and the menu from `relation_options.ranked_options` with the queue's admissibility check. Verify: on a synthetic vault, every request's menu equals the queue's options for the same item, and no Markdown is parsed.
- [x] 2.2 Call the producer from the sensed tick: changed pages first, then a persisted cursor; fixed page and link bounds per tick; one replaceable scope per source page; ingestion into one projection table. Verify: a 50,000-page synthetic graph costs the same per tick; an edited page is submitted first and its stale requests are removed.

## 3. Delivery

- [x] 3.1 Annotate relation-queue candidates from the projection: relation key, probability, reading id and instrument id, for relation verdicts only. Verify: the queue stays graph-native under the existing bounded-work scenarios, and with no readings its response is byte-identical to the current one.
- [x] 3.2 Accept a sensed proposal through `connect_memory(operation="accept-relation")`: a typed bullet for a body wikilink, an exact bullet replacement for a plain-link or generic bullet, the page hash bound, and the reading id in the audit reason. Verify: tests for both accept shapes, drift refusal, and accepting a different menu option.

## 4. Admission and measurement

- [x] 4.1 Define the admission evidence artifact (counts, Wilson intervals, `propose_min`, CPU seconds per case, private and file-backed memory, digests; no vault text) and check it at admission. Verify: a pin below the bar and a map without evidence are both refused with a named reason.
- [ ] 4.2 Record the private distillation spike in design "Measured": dev and held-out judged precision and yield against the frontier ceiling with paired intervals, CPU seconds per link, peak memory, and the Cloud-cell estimate.
- [x] 4.3 Run the producer, worker and queue end to end on a disposable vault copy, with the experimental model injected through the test seam rather than a pin. Verify: proposals appear in the queue, one accept writes through the governed path, and nothing else in the vault changes.

## 5. Related changes and closure

- [x] 5.1 Amend `add-sensed-epistemic-model` in place: D1's `relation.applies` contract is replaced by `relation.type` here, and task 9.4's relation typing is re-laned to this change.
- [x] 5.2 Run the tests, `uv run python scripts/validate-public-artifacts.py --repository` and `openspec validate --all --strict`.

A shippable sensor-model pin is not part of this change. It needs a training set without private vault content and its own admission evidence (design D7), and lands as its own change.
