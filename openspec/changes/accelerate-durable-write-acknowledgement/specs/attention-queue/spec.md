## ADDED Requirements

### Requirement: Deferred Write Advisory Results Stay Outside Review Queues

A deferred write-advisory job and its result record SHALL NOT be an attention,
activation, relation, adoption, or due-state item. It SHALL contribute no
category, rank, count, fusion input, continuation, bootstrap field, carrier, or
unsolicited later response. Exact result lookup MAY return ready individual
`exomem://review/write-advisory/<id>` references; those references SHALL retain
the existing fingerprint-bound triage and family-disposition semantics without
enrolling the operational result record in a queue.

#### Scenario: Background advisory becomes ready

- **WHEN** a deferred advisory job transitions from pending to ready
- **THEN** ordinary attention, activation, audit, and due-state outputs are unchanged by the job or result record
- **AND** the result is reachable only through its exact opaque result reference

#### Scenario: Ready advisory is triaged

- **WHEN** exact result lookup returns a ready write-advisory reference and the caller dismisses that reference
- **THEN** the existing write-advisory decision namespace records the exact fingerprint decision
- **AND** no queue item is created or reordered
