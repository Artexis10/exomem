## ADDED Requirements

### Requirement: Compact bootstrap names the route to the next due batch

The compact core's due-state summary SHALL report the per-category visible and outstanding counts and one `next` route, `review_memory(mode="attention", categories=[<category with the most visible items>], limit=<batch>)`. The route SHALL be built from the category name, with no per-category table. The engagement guidance SHALL teach the batch loop: list a batch, group it by topic, act through the existing writers, and check that the outstanding count fell; and it SHALL state that a dismissal is not completion. The summary SHALL stay within the compact bootstrap byte ceiling.

#### Scenario: A session sees how to start on the backlog

- **WHEN** a compact bootstrap runs on a vault with outstanding `unprocessed_source` items as its largest visible category
- **THEN** the due-state summary carries the per-category counts and a `next` route naming `unprocessed_source`
