## MODIFIED Requirements

### Requirement: Model polarity labels are admitted asynchronous enrichment

A `corpus_contradictions` proximity pair MAY carry a model polarity label.

- **Fields.** The label is `meta.polarity` from the closed set `contradict` / `refine` / `duplicate` / `neutral`, with `meta.polarity_score`, `meta.polarity_method: "nli"`, `meta.polarity_model_digest`, `meta.polarity_label_map_version` and `meta.polarity_reading` (the reading id).
- **Source.** It SHALL be read from a current `pair.relation` reading in the readings ledger, produced by an admitted instrument over the two pages' extracted claim texts (extractor `page-claim-v1`). The ledger's `contradicts`, `refines`, `restates` and `neutral` map to `contradict`, `refine`, `duplicate` and `neutral`. An `abstain` reading SHALL attach no label.
- **No model in the request.** The audit request SHALL NOT load or run a model. A pair without a current reading SHALL be queued for sensing and served without a label.
- **Neutral.** `neutral` means the admitted NLI map found no symmetric contradiction or qualifying entailment relation. It SHALL NOT be rendered or interpreted as proof that the claims are topically unrelated.
- **Heuristic and write path.** The lexical heuristic SHALL NOT produce queue polarity metadata. The synchronous write path SHALL invoke no polarity classification, and write-time warnings SHALL carry no polarity clause.
- **Signal version.** The label SHALL record the `signal_version` it was computed against. A label whose recorded signal_version differs from the entry's SHALL be dropped, not served.
- **No side effects on the entry.** Attaching, changing, or dropping a label SHALL NOT change the entry's `meta.signal_version`, its `meta.provenance`, its position under the queue's ordering rules, or the cap and omitted-count accounting. A recorded triage decision SHALL NOT resurface because a label arrived, changed, or was dropped.
- **Asserted pairs.** Asserted pairs SHALL NOT carry a model polarity label — the author's assertion outranks a model's guess.
- **Pair stance.** The model polarity label is distinct from the reader-recorded competing-alternatives pair stance, which remains a triage disposition under its own contract and is unaffected by this requirement.

#### Scenario: The label arrives on the sweep, not the write

- **WHEN** a write lands a proximity pair, the sensor worker records a current reading, and the next audit pass runs
- **THEN** the write response carried no polarity, and after the pass the queue entry carries the label with its digest, label-map version and reading id

#### Scenario: The audit request runs no model

- **WHEN** an audit pass surfaces a proximity pair with no current reading
- **THEN** no model is loaded or run in the request, the entry carries no label, and the pair is queued for sensing

#### Scenario: Labelling alone resurfaces nothing and moves nothing

- **WHEN** a dismissed proximity entry gains a `contradict` label
- **THEN** the dismissal stands, `signal_version` is unchanged, and the
  entry's rank relative to every other entry is unchanged

#### Scenario: A stale label is dropped, not served

- **WHEN** an entry's content changes so its `signal_version` no longer
  matches the one its label was computed against
- **THEN** the entry is served without the label until a reading covers the
  new content

#### Scenario: Neutral does not claim unrelatedness

- **WHEN** the reading is `neutral` for two compatible but non-entailing
  claims
- **THEN** the queue describes the NLI relation as neutral and does not call the
  pair unrelated

#### Scenario: The heuristic never wears the verifier's name

- **WHEN** no admitted instrument has read the pair and the audit contradiction
  pass runs with the claim subsystem enabled
- **THEN** entries carry no `meta.polarity` at all — no heuristic-method
  label is written
