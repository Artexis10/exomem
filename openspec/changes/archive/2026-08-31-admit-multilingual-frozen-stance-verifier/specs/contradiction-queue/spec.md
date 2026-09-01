## MODIFIED Requirements

### Requirement: Model polarity labels are admitted asynchronous enrichment

A `corpus_contradictions` proximity pair MAY carry a model polarity label —
`meta.polarity` from the closed set `contradict` / `refine` / `duplicate` /
`neutral`, with `meta.polarity_score`, `meta.polarity_method: "nli"`,
`meta.polarity_model_digest`, and `meta.polarity_label_map_version` — produced
only by an admitted frozen verifier. `neutral` means that the admitted NLI map
found no symmetric contradiction or qualifying entailment relation; it SHALL NOT
be rendered or interpreted as proof that the claims are topically unrelated.
The lexical heuristic SHALL NOT produce queue polarity metadata. The label SHALL
be produced only on the asynchronous audit/sweep path; the synchronous write path
SHALL invoke no polarity classification, and write-time warnings SHALL carry no
polarity clause.

The label SHALL record the `signal_version` it was computed against; a label
whose recorded signal_version differs from the entry's SHALL be dropped, not
served. Attaching, changing, or dropping a label SHALL NOT change the entry's
`meta.signal_version`, its `meta.provenance`, its position under the queue's
ordering rules, or the cap and omitted-count accounting; a recorded triage
decision SHALL NOT resurface because a label arrived, changed, or was dropped.
Asserted pairs SHALL NOT carry a model polarity label — the author's assertion
outranks a model's guess. The model polarity label is distinct from the
reader-recorded competing-alternatives pair stance, which remains a triage
disposition under its own contract and is unaffected by this requirement.

#### Scenario: The label arrives on the sweep, not the write

- **WHEN** a write lands a proximity pair and the next audit pass runs with the
  verifier admitted
- **THEN** the write response carried no polarity, and after the pass the queue
  entry carries the label with its digest and label-map version

#### Scenario: Labelling alone resurfaces nothing and moves nothing

- **WHEN** a dismissed proximity entry gains a `contradict` label
- **THEN** the dismissal stands, `signal_version` is unchanged, and the entry's
  rank relative to every other entry is unchanged

#### Scenario: A stale label is dropped, not served

- **WHEN** an entry's content changes so its `signal_version` no longer matches
  the one its label was computed against
- **THEN** the entry is served without the label until the verifier labels the
  new content

#### Scenario: Neutral does not claim unrelatedness

- **WHEN** the verifier emits `neutral` for two compatible but non-entailing
  claims
- **THEN** the queue describes the NLI relation as neutral and does not call the
  pair unrelated

#### Scenario: The heuristic never wears the verifier's name

- **WHEN** the verifier is refused and the audit contradiction pass runs with
  the claim subsystem enabled
- **THEN** entries carry no `meta.polarity` at all — no heuristic-method label is
  written
