## ADDED Requirements

### Requirement: Model stance labels are asynchronous provenance-marked enrichment

A `corpus_contradictions` proximity pair MAY carry a model stance label —
`contradict`, `refine`, `duplicate`, or `unrelated` — produced by an admitted
frozen verifier, attached as entry metadata naming the method, model digest,
and label-map version. Stance SHALL be produced only on the asynchronous
audit/sweep path; the synchronous write path SHALL invoke no stance
classification, and write-time warnings SHALL carry no stance clause. Attaching
or changing a stance SHALL NOT change the entry's `meta.signal_version`, its
`meta.provenance`, its position under the queue's ordering rules, or the cap
and omitted-count accounting; a recorded triage decision SHALL NOT resurface
because a stance arrived or changed. Asserted pairs SHALL NOT be
stance-labelled — the author's stance already outranks a model's.

#### Scenario: Stance arrives on the sweep, not the write

- **WHEN** a write lands a proximity pair and the next audit pass runs with the
  verifier admitted
- **THEN** the write response carried no stance, and after the pass the queue
  entry carries the label with its digest and label-map version

#### Scenario: Labelling alone resurfaces nothing and moves nothing

- **WHEN** a dismissed proximity entry gains a `contradict` stance
- **THEN** the dismissal stands, `signal_version` is unchanged, and the
  entry's rank relative to every other entry is unchanged

#### Scenario: The write path is stance-free even when the verifier is admitted

- **WHEN** the verifier is installed, pinned, and admitted and a draft overlaps
  an active note
- **THEN** the write-time overlap warning is exactly the proximity text, with
  no claim-level clause
