## MODIFIED Requirements

### Requirement: Unified Review Surface Composed From The Epistemic Queues

The default `attention` category union SHALL preserve the existing review queues and the already-shipped `relation_debt` queue while adding `bridge_review` and deterministic `entity_type_unregistered` audit findings. Its default category and tiebreak-preference order SHALL retain the existing ordering and SHALL compose unregistered entity types without giving them a dismiss-to-silence path. The broader registered attention category set SHALL continue to admit its existing typed semantic categories. `attention` SHALL consume one audit pass over its selected categories and SHALL remain read-only.

`bridge_review` SHALL use read-only reference resolution; it SHALL not write canonical governance facts or review state during scanning. A release grant's bridge SHALL surface per approved audience for only these generic, bridge-path-anchored causes: due review date, bridge edited/stale approval, source or relevant restriction changed, and source unavailable/ambiguous. Detail, metadata, related paths, review context, and public responses SHALL not disclose restricted source title, path, ref, or other provenance.

#### Scenario: Default attention composes the effective queue union

- **WHEN** `attention` is called without a category filter
- **THEN** it composes bridge review, contradiction, stale-review, unprocessed source, relation-debt, and unregistered-entity-type findings without removing any existing queue
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: Category subset and registered-category validation

- **WHEN** `attention` is called with `categories=["entity_type_unregistered"]`
- **THEN** only unregistered-entity-type items are surfaced
- **AND** the registered existing categories remain accepted while an unregistered category raises a `ValueError` naming the valid set

#### Scenario: Unregistered type resolves only from state

- **WHEN** an unregistered-type attention item is dismissed or snoozed without changing the registry or pages
- **THEN** unchanged audit state remains eligible to surface
- **AND** every reason reports a `decision` key, with the recorded action on ordinary reasons and `null` on state-resolved-only reasons
- **AND** registering the type or moving the pages clears the item on the next pass

#### Scenario: Source drift produces a private bridge finding

- **WHEN** an approved dependency changes, is deleted, or resolves ambiguously
- **THEN** the bridge surfaces with a generic `bridge_review` cause and no source provenance
