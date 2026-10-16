## ADDED Requirements

### Requirement: Held collection view corrections are a default attention category

The audit SHALL register a `collection_view_corrections` category with one finding per held view correction of a store collection, naming its `Held/` view path, its collection, its held reference and its code. The category SHALL join the default attention union last, after `transient_state_review`. A held correction is the owner's own edit waiting on the owner, which neither expires nor is inferred, so it ranks below every existing default queue. The category SHALL list only the held corrections released to the requesting audience, so attention for a caller that cannot see a correction SHALL equal attention when that correction is absent. A vault without a store collection SHALL produce no finding and SHALL load no store library to compute the category. The category SHALL NOT join the due-state projection or any carrier. The existing item decisions and family dispositions SHALL apply.

This delta to the default union is stated here rather than as a MODIFIED requirement against `Unified Review Surface Composed From The Epistemic Queues`, because unarchived changes already carry one and another would collide at archive-sync.

#### Scenario: A held stale-buffer save appears in attention

- **WHEN** an owner saves an item view from a stale editor buffer and edit-back holds the save as `VIEW_CONFLICT`
- **THEN** the default attention view lists the correction's `Held/` view path under `collection_view_corrections`
- **AND** discarding the correction removes the item on the next attention read

#### Scenario: A withheld correction is absent

- **WHEN** a held correction targets an item that a caller's release policy withholds
- **THEN** that caller's attention is the same as attention when the correction does not exist
