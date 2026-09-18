## MODIFIED Requirements

### Requirement: Derived activation index
The product SHALL maintain an activation index as a disposable derived sidecar under
the machine-local state root, built only from governed structure: entity pages (title,
`aliases`, entity type, attributes), the pages the effective activation conventions
registry admits as `hub` and `resource` anchors, active Planning items (title, kind,
tags, execution pointer), Records collection manifests (title, item schema field names,
`claims`), and project keys. The index walk SHALL take the directories it skips and the
append-only raw-material trees it excludes from the product's shared layout
definitions, not from a list of its own, and SHALL never admit a page inside an
append-only tree as an anchor. Each anchor row SHALL carry a canonical ref, an anchor
kind, a lifecycle, a structural signature extracted without any generative model,
alias/lexical terms, an optional signature embedding produced by the configured
embedding backend, typed link summaries and the index generation. The index SHALL be
rebuildable from the vault alone, SHALL be updated incrementally on the recall freshness
checkpoint with a write-generation token, SHALL never be read by ordinary recall lanes,
and SHALL never be treated as canonical truth.

#### Scenario: Rebuild equals incremental
- **WHEN** an index built incrementally across a sequence of vault writes is compared
  with an index rebuilt from scratch at the final vault state
- **THEN** the two contain the same anchor rows, aliases, categories and links, and the
  same signature text

#### Scenario: Missing index is built or reported, never faked
- **WHEN** `activate_context` is called and no index exists for the vault
- **THEN** the unmanaged runtime builds it inline within the request budget, while a
  managed runtime schedules a single-flight background build and returns a packet with
  `abstained: true` and `abstention.reason = "index_warming"` for that request only

#### Scenario: Structured items stay out of recall
- **WHEN** a Planning item or Records item exists in the vault
- **THEN** it may appear as an activation anchor, but the activation index does not
  make it a candidate of `ask_memory`

#### Scenario: Shipped conventions admit the same anchors as before
- **WHEN** a vault has no conventions override
- **THEN** the index holds `resource` anchors for pages under `Products/` and `Systems/`
  and `hub` anchors for pages tagged `hub` outside the append-only trees, as it did
  before the registry existed
