# planning

## ADDED Requirements

### Requirement: Planning items may link to their results

A Planning item MAY declare `results`: at most 16 link values naming the record, plan item or page where its outcome lives. A collection SHALL accept `results` only after declaring it in `item_schema.fields`, as `motivation` requires. Each value SHALL be canonicalised and resolved as a collection `link` value is, and an unresolved value SHALL commit with a warning. `results` SHALL NOT satisfy `parent` or `area` or feed hierarchy edges.

#### Scenario: A completed outcome links to its record

- **WHEN** an update completes a work item and sets `results` to a record's canonical ref
- **THEN** the item commits with the ref, and the record's citations list the item

#### Scenario: More than sixteen results is refused

- **WHEN** an add or update supplies 17 `results`
- **THEN** validation refuses before canonical publication, naming the limit

### Requirement: Completion without a linked result is advised, not refused

An update that moves an item to a status whose registry `class` is `done` and leaves `results` empty SHALL commit and report `no_result_linked`. The status class SHALL be read from the `planning-values` registry, never from a status key.

#### Scenario: Completion without a result

- **WHEN** an update completes an item whose `results` is empty
- **THEN** the update commits and its reply reports `no_result_linked`

### Requirement: A cited page may list plan items as derived pointers

A page MAY list the Planning items whose canonical `motivation` or `results` values name it, read through the `page-citations` seam under the reader's visibility. Such a listing SHALL NOT be a graph edge, a recall hit or a stored relation, and plan items SHALL stay outside semantic recall and the relation graph.

#### Scenario: A cited page lists the plan item

- **WHEN** a Planning item's `motivation` names a page the reader may see, and the reader may see the item
- **THEN** reading that page lists the item among its citations, and the item is still not a recall hit and creates no graph edge
