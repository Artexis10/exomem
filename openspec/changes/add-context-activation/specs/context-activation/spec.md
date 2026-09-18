# context-activation

## ADDED Requirements

### Requirement: Read-only activation operation
The product SHALL expose a read-only operation `activate_context` that accepts a raw
user turn (`turn`, non-empty text), an optional `max_chars` (default 4000, clamped
to 500..8000), an optional declared `purpose`, and an optional `include_timings`
flag, and returns a working-memory packet. The operation SHALL be reachable over the
same leaf function on MCP, the CLI (`exomem activate "<turn>"`) and the personal REST
facade (`/api/activate_context`). It SHALL perform no vault write, SHALL NOT change the
hits, ordering or envelope of `ask_memory`/`find` for any input, and SHALL run no model
other than the retrieval scorers ordinary recall already runs.

#### Scenario: Same packet on every door
- **WHEN** the same turn is submitted through MCP, the CLI and the REST facade against
  the same vault state and index generation
- **THEN** the three responses carry the same anchors, roles, units, pointers and
  budget accounting

#### Scenario: Ordinary recall is untouched
- **WHEN** `ask_memory` is called with any arguments before and after this change is
  installed and the activation index exists
- **THEN** its hits, ordering and envelope are byte-identical

#### Scenario: Kill switch abstains without building
- **WHEN** `EXOMEM_DISABLE_WORKING_SET` is set in the server process
- **THEN** `activate_context` returns a packet with `abstained: true` and
  `abstention.reason = "disabled"`, no activation index is created or opened, and the
  tool remains on the surface so the tool-surface digest is independent of the switch

### Requirement: Derived activation index
The product SHALL maintain an activation index as a disposable derived sidecar under
the machine-local state root, built only from governed structure: entity pages (title,
`aliases`, entity type, attributes), hub pages outside the immutable raw-material
folders (`Sources/`, `Evidence/`), `Products/` and `Systems/` pages,
active Planning items (title, kind, tags, execution pointer), Records collection
manifests (title, item schema field names, `claims`), and project keys. Each anchor
row SHALL carry a canonical ref, an anchor kind, a lifecycle, a structural signature
extracted without any generative model, alias/lexical terms, an optional signature
embedding produced by the configured embedding backend, typed link summaries and the
index generation. The index SHALL be rebuildable from the vault alone, SHALL be updated
incrementally on the recall freshness checkpoint with a write-generation token, SHALL
never be read by ordinary recall lanes, and SHALL never be treated as canonical truth.

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

### Requirement: Categorical anchor evidence and resolution
Anchor candidates SHALL carry only categorical evidence kinds. Contact kinds —
`exact_alias`, `lexical_overlap`, `vector_band`, `claims_match` and `retrieval` —
establish that the turn reached the anchor; qualifier kinds — `category_match`,
`graph_corroboration` and `usage_prior` — strengthen an anchor the turn already reached
and SHALL never create a candidate on their own. No float score SHALL appear in the
packet. An anchor SHALL resolve as `resolved` when it carries `exact_alias` or at least
two independent kinds other than `usage_prior`, at least one of them a contact kind; as
`partial` when it carries exactly one kind other than `usage_prior`; the turn SHALL be
`ambiguous` when two or more resolved anchors of the same anchor kind have disjoint
typed-link neighbourhoods (resolved anchors of different kinds are complementary);
otherwise, when no anchor is `resolved`, the turn SHALL be `unresolved` and the
operation SHALL abstain with an empty packet. Lexical overlap SHALL ignore stopwords. `usage_prior` SHALL only break ties between
otherwise equal candidates and SHALL never contribute to the two-kinds rule.
`claims_match` SHALL be computed with the existing collection-claims routing and
`graph_corroboration` SHALL count a typed edge between two candidates even when both
already appear in ordinary recall.

#### Scenario: Two kinds resolve a resource anchor
- **WHEN** a turn mentions a resource whose profile page title matches lexically and
  whose Records collection claims cover the turn's terms
- **THEN** the anchor resolves with evidence `[lexical_overlap, claims_match]`

#### Scenario: Ambiguous domain is reported, not guessed
- **WHEN** a turn's terms resolve two hub anchors whose typed-link neighbourhoods share
  no page
- **THEN** the packet status is `ambiguous`, both anchors are listed under
  `ambiguity` with their neighbourhood sizes, and no role lane runs for either

#### Scenario: Negative twin abstains
- **WHEN** a turn is lexically similar to an anchor's domain but carries no alias, no
  claims coverage and no corroborating kind
- **THEN** no anchor is `resolved`, the packet is `abstained: true` with
  `abstention.reason = "unresolved"`, and `units` and `pointers` are empty

#### Scenario: Usage never resolves
- **WHEN** a candidate carries only `usage_prior` and `vector_band`
- **THEN** it is at most `partial`

### Requirement: Bounded role lanes and the working-memory packet
For each resolved anchor the operation SHALL select context roles per the
`context-roles` capability and run one bounded lane per selected role over existing
primitives only: semantic units filtered by category within the anchor neighbourhood,
Records collection items for collections claiming the anchor, active Planning items
linked to the anchor, entity or profile page facets, `graph_context` under a
traversal profile from resolved anchors at depth at most 2 (depth 1 from `partial`
anchors), and evidence pointers. The packet SHALL contain `anchors[]` (ref, title,
kind, status, evidence), `roles[]`, `units[]` (ref, role, text of at most 360
characters cut only at a boundary that leaves no unclosed wikilink, lifecycle,
updated, provenance), `pointers[]` (ref, title, why), `current_state[]` (anchor,
source, as_of, `statement` of at most 200 characters stating the observed status in
the source's own words), `missing[]` (role, reason), `ambiguity[]`,
`budget {limit_chars, used_chars}`, `generation {freshness_key, index_generation,
roles_hash}` and `abstained`. `used_chars` SHALL count every prose field of the
packet (unit text, `current_state[].statement`, pointer title and why) and SHALL
never exceed `max_chars`. Units SHALL be emitted before pages, pages beyond the
budget SHALL become pointers, and a superseded unit SHALL be marked `superseded`
with its active successor named rather than presented as current. A lane that
reaches its read limit before exhausting the anchor neighbourhood SHALL report
`missing[] {role, reason: "lane_truncated"}`, and a lane whose items fit neither as
units nor as pointers within `max_chars` SHALL report `missing[] {role, reason:
"budget"}`; the packet never drops material silently. The packet SHALL NOT carry the
`due_state` block and SHALL NOT read or advance the due-state emission ledger; recall
remains the only `due_state` carrier.

#### Scenario: Budget holds under a large neighbourhood
- **WHEN** the selected lanes yield more candidate text than `max_chars`
- **THEN** the packet's `used_chars` is at most `max_chars`, units are kept in role
  priority order, and the overflow is represented only as pointers

#### Scenario: Superseded knowledge is marked
- **WHEN** a role lane yields a unit whose page carries `status: superseded` and a
  `superseded_by` target
- **THEN** the unit either is omitted in favour of the successor's unit or appears with
  `lifecycle: superseded` and `provenance.superseded_by` set; it is never emitted as
  active

#### Scenario: Current state comes from Records first
- **WHEN** a resolved resource anchor is claimed by a Records collection whose latest
  item states the resource's status
- **THEN** `current_state[]` carries that status as `statement` with `source: records`
  and the item's `observed_on`, and the `current_state` role lane does not substitute
  an older prose note for it

#### Scenario: A truncated lane is reported, not hidden
- **WHEN** a role lane's read limit is reached while in-neighbourhood units remain
- **THEN** `missing[]` carries `{role, reason: "lane_truncated"}` for that lane

#### Scenario: Activation never consumes the due-state emission
- **WHEN** `activate_context` is called and `ask_memory` is then called on the same
  vault in the same process with a due-state block pending
- **THEN** the `ask_memory` response carries the same `due_state` block it would have
  carried had `activate_context` not been called

#### Scenario: Abstained packet injects nothing
- **WHEN** the turn is `unresolved`
- **THEN** `units`, `pointers` and `current_state` are empty and `budget.used_chars`
  is 0

### Requirement: Governance and egress
Every lane SHALL run under the governance release plane that `find` uses, and every
served packet SHALL be passed through an egress guard that receives the same release
object as hit projection, so that no anchor, unit, pointer, provenance string,
`missing[]` entry or `current_state` entry names a withheld page, including inside
wikilink syntax in `units[].text` and `current_state[].statement`. The guard SHALL
run whether or not the `retrieval` evidence was available: a recall failure degrades
one evidence kind and never bypasses the guard, and a failure of the release plane
itself SHALL abstain rather than serve. The declared `purpose` SHALL be honoured
exactly as it is for recall and SHALL never enter ranking or a cache key.

#### Scenario: Recall failure does not bypass the guard
- **WHEN** an active policy withholds a page from the caller's audience and the
  internal recall call raises
- **THEN** the packet is still guarded and names nothing from the withheld page

#### Scenario: A permitted wikilink in prose survives the guard
- **WHEN** a permitted unit's text contains a wikilink to a page the caller's audience
  may read, or to a page that does not exist
- **THEN** the unit is served unchanged; only a wikilink to a withheld page drops it

#### Scenario: Withheld page never leaks through unit prose
- **WHEN** a permitted unit's text contains a wikilink to a page withheld from the
  caller's audience, spelled by filename stem, by page title, with a display alias
  (`[[page|label]]`) or with a heading (`[[page#section]]`)
- **THEN** the served packet contains no reference to that page in any unit's text

#### Scenario: An ungoverned vault never depends on the activation sidecar for release
- **WHEN** no governance policy is active, no page is withheld and the packet carries
  prose wikilinks
- **THEN** the guard serves the packet without consulting the page-name map, and a
  sidecar fault cannot abstain the read

#### Scenario: Withheld page never leaks through provenance
- **WHEN** a governed audience withholds a page that is a typed neighbour of a resolved
  anchor
- **THEN** the packet contains no reference to that page in any field, and the anchor's
  `graph_corroboration` evidence, if it depended on that page, is dropped

### Requirement: Freshness and caching
Packets SHALL be keyed on the recall freshness key, the activation index generation,
the role-registry hash and a digest of the retrieval refs the release plane admitted
for the request, so that a packet compiled with one audience's retrieval evidence is
never served to another. When the index generation is behind the freshness
checkpoint, the operation SHALL either refresh the index within the request budget or
mark the packet `generation.index_stale: true`; it SHALL never serve a packet whose
generation block misreports the index generation it was built from.

#### Scenario: One audience's packet is never served to another
- **WHEN** two requests share turn, `max_chars`, freshness key, index generation and
  roles hash but the release plane admitted different retrieval refs for them
- **THEN** the second request is compiled from its own retrieval refs, not served the
  first request's cached packet

#### Scenario: Write invalidates a cached packet
- **WHEN** a packet was served and a governed write then adds an alias to an entity
  named in the next turn
- **THEN** the next call reflects the new alias and reports a newer
  `index_generation`

### Requirement: Instrumentation and latency bound
The operation SHALL record timing spans `working_set.index`, `working_set.resolve`,
`working_set.roles`, `working_set.lanes.<role>` and `working_set.budget` under the
existing timings collector, SHALL satisfy `sum(stages) <= total_ms`, and the CI latency
gate SHALL bound a warm activation on the model-free synthetic corpus by an absolute
ceiling and by a 2,000→8,000-note scaling ratio, both pinned in the gate alongside
the referents ceilings.

#### Scenario: Stages partition the total
- **WHEN** `activate_context` runs with `include_timings=true`
- **THEN** every recorded stage is a registered interval and the stages sum to at most
  the reported total

### Requirement: Surface parity and pins
The tool SHALL be present on the canonical MCP surface, the CLI and the REST facade
with one documented contract; the schema fidelity fixture, both tool-surface digests,
the hosted plugin and candidate trees, `docs/capabilities.md`, the README tool table
and the skill scaffold SHALL be regenerated in the same change, and the scaffold
no-leak gate SHALL pass.

#### Scenario: Schema fixture matches the served surface
- **WHEN** the schema fidelity test runs after this change
- **THEN** the served tool list and the pinned fixture agree and include
  `activate_context`
