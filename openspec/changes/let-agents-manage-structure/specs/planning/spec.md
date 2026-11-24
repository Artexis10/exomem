## ADDED Requirements

### Requirement: A Planning restructure keeps the Planning core
`change_plans` `restructure` SHALL follow the shared restructure, retirement, automatic-bound and shape-history requirements of `structured-collections`. Its field steps SHALL apply only to fields that a collection declares beyond the Planning core. The core fields, lifecycle and horizon semantics SHALL stay as the Planning profile defines them, and a step that names a core field SHALL be refused naming it.

#### Scenario: A core field cannot be renamed
- **WHEN** a Planning restructure renames the `status` field
- **THEN** the server refuses naming the core field, and the collection is unchanged

### Requirement: A Planning merge or split keeps item identity and references
`merge_into` and `split_out` SHALL keep every Planning item identity. `parent` and `area` name an item in the same collection. So a split SHALL refuse, naming the items, when an item would end apart from its `parent` or `area`. A merge SHALL carry each copied item's `parent` and `area` references to the target collection. The retired source SHALL keep its own items and references, so an undo restores them by restoring the source.

#### Scenario: A merge keeps item identity
- **WHEN** one Planning collection merges into another
- **THEN** every copied item keeps its Planning item identity in the target, and its `parent` and `area` resolve there
- **AND** a stable reference to the item in the source still resolves, marked retired with the target as its successor

#### Scenario: A split that would orphan a child is refused
- **WHEN** a split would move a work item without its parent initiative
- **THEN** the server refuses naming the work item and its parent, and nothing moves

### Requirement: A restructure keeps every Planning item a readable file
A restructure SHALL keep every item an ordinary readable Markdown file under the Planning layer.

#### Scenario: A restructured item stays a Markdown file
- **WHEN** a Planning restructure merges or splits a collection
- **THEN** every item is still an ordinary Markdown file under the Planning layer
