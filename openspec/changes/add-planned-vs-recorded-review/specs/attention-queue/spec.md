## ADDED Requirements

### Requirement: Plan-progress review is a standalone mode outside the Epistemic Inbox

Planned-versus-recorded review SHALL be a standalone read-only `review_memory` mode and SHALL NOT be an attention category. It SHALL NOT contribute items to the default `attention` union or to any registered typed semantic category, SHALL NOT participate in Reciprocal Rank Fusion or any other cross-queue ranking, SHALL NOT mint `exomem://review/<id>` references or signal fingerprints, and SHALL NOT be reachable by `review_item_context`, `triage_memory`, snooze, dismiss, or any other disposition.

The reason is the adjudication boundary the attention queue already draws. Inbox items exist so a reviewer can dispose of a proposed finding; plan-progress divergence is a measured fact about authored intent and recorded observation that the human interprets directly. Enrolling it would also require a rank, and ranking plans by divergence would be exactly the derived judgment the review refuses to make. The existing attention categories, ordering, fusion, dedup, and truncation behaviour SHALL remain unchanged by this mode.

#### Scenario: Default attention union is unchanged
- **WHEN** `review_memory(mode="attention")` runs over a vault whose Planning items diverge from their recorded evidence
- **THEN** the composed queue union, its default category order, and its counts are exactly what they were before plan-progress review existed
- **AND** no plan, evidence binding, or divergence count appears as an attention item

#### Scenario: Plan-progress produces no triageable reference
- **WHEN** a plan-progress review returns items with unresolved bindings and zero completion observations
- **THEN** the response carries plan references rather than review references
- **AND** no returned identifier can be snoozed, dismissed, or otherwise triaged

#### Scenario: Review state store is untouched
- **WHEN** a plan-progress review runs
- **THEN** no review-state entry, fingerprint, or disposition is read for adjudication or written
