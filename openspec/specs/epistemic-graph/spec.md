# epistemic-graph Specification

## Purpose
TBD - created by archiving change add-epistemic-graph. Update Purpose after archive.

## Requirements

### Requirement: Derived Epistemic Graph Sidecar
The system SHALL maintain a rebuildable SQLite graph sidecar derived from Markdown, frontmatter, wikilinks, source/evidence references, supersession fields, semantic blocks, and existing media metadata. Markdown files SHALL remain the canonical source of truth. The sidecar SHALL store typed nodes, typed edges, provenance, schema version, and source freshness metadata, and MUST NOT require a graph database service.

#### Scenario: Graph builds from governed Markdown
- **WHEN** the graph index runs over a governed KB containing notes with `sources`, `supersedes`, wikilinks, and semantic sections
- **THEN** the sidecar contains nodes for files and semantic blocks
- **AND** it contains typed edges such as `derived_from`, `supersedes`, `links_to`, and `evidenced_by` where those relations are present in the source files

#### Scenario: Sidecar can be rebuilt from files
- **WHEN** the graph sidecar is deleted and the graph index is rebuilt from the same unchanged Markdown files
- **THEN** graph context over the same seed returns equivalent nodes, edges, and provenance
- **AND** no Markdown file needs to be modified to recover the graph

### Requirement: Explicit Relationship Vocabulary
The graph SHALL consume the portable core and governed vault-extension relation
registry rather than a duplicated relation enum. Accepted typed edges SHALL
retain origin and source provenance and SHALL NOT store authority or confidence
floats on notes. Explicit unknown relation observations SHALL be retained as
unregistered derived edges for audit and later re-resolution, but SHALL NOT be
accepted into normal traversal or assigned a family, inverse, direction, or
epistemic meaning until registered.

#### Scenario: Relationship provenance is returned
- **WHEN** a graph edge is derived from a note's frontmatter, semantic unit, or body
- **THEN** graph lookups return its raw and canonical relation identity, source path, origin method, and source anchor or span when available
- **AND** the relation can be traced back to the file content that produced it

#### Scenario: Unsupported relation label is not persisted
- **WHEN** an explicit typed location contains an unrecognized optional relation label
- **THEN** the graph persists its raw observation and provenance with `registry_status="unregistered"`
- **AND** indexing continues while normal traversal excludes that edge from accepted typed semantics

### Requirement: Read-Only Graph Context Surface
The system SHALL expose a read-only `graph_context` operation through the single command registry on MCP, REST, and CLI. The operation SHALL accept a path or query seed plus bounded traversal controls such as depth, relation-type filters, node-type filters, and caps. The response SHALL include seed nodes, related nodes, edges, provenance, graph availability, and explicit truncation entries whenever caps omit content.

#### Scenario: Graph context returns a bounded neighborhood
- **WHEN** `graph_context` is called for a note with depth `1`
- **THEN** it returns the seed note, directly related nodes, typed edges, and provenance
- **AND** no files under the vault are created, modified, moved, or deleted

#### Scenario: Graph context is exposed consistently
- **WHEN** the command registry is inspected
- **THEN** `graph_context` is available as an MCP tool, `/api/graph_context` REST route, and CLI subcommand
- **AND** all three surfaces call the same leaf function

### Requirement: Relation Suggestions Are Propose-Only
The system SHALL expose relation suggestions as proposal output, not durable accepted facts. `suggest_relations` SHALL accept an existing path or draft title/body and return candidate edges with relation type, method, evidence paths/spans, and explanation text. It MUST NOT write Markdown, mutate the graph sidecar as accepted state, change supersession fields, or change `find` ranking.

#### Scenario: Suggestions do not mutate the vault
- **WHEN** `suggest_relations` is called for an existing note
- **THEN** it returns candidate typed relations with evidence
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: Embeddings unavailable still yields deterministic suggestions
- **WHEN** embeddings are disabled or unavailable
- **THEN** `suggest_relations` still returns deterministic candidates from wikilinks, frontmatter, shared sources, and entity mentions when available
- **AND** embedding-based candidates are omitted with an availability indication rather than causing the operation to fail

### Requirement: Model-Backed Graph Suggestions Respect Pure Substrate
Any model-backed relation, contradiction, polarity, or claim-classification path SHALL be optional, default-off, and soft-failing. Such paths SHALL be labelled as measurement and SHALL only propose candidate edges for review; they MUST NOT author note text, auto-accept relations, auto-supersede notes, or run as a server-side reasoning agent.

#### Scenario: Default graph indexing uses no reasoning model
- **WHEN** the graph sidecar is built with default configuration
- **THEN** graph indexing uses deterministic extraction and available measurement sidecars only
- **AND** no generative or reasoning model is invoked

#### Scenario: Optional model failure does not break graph context
- **WHEN** optional model-backed relation suggestion is enabled but the model cannot load
- **THEN** graph context remains available from deterministic graph data
- **AND** the response reports the optional suggestion path as unavailable

### Requirement: Structural Relation Suggestions From Authored Semantic Units

`suggest_relations` SHALL return deterministic structural candidates derived
from authored semantic units held in the typed graph sidecar, in addition to the
existing wikilink, frontmatter-source, shared-source and embedding-proximity
candidates. The structural methods SHALL be `unit_relation_lift`,
`shared_open_question`, and `shared_resolution_target`. Every structural
candidate SHALL name a page-level target and SHALL carry a relation type, a
method, and evidence. The operation MUST NOT write Markdown, mutate graph edges,
change supersession fields, or change `find` ranking.

#### Scenario: A typed unit relation is lifted to a page-level proposal

- **WHEN** a page carries a semantic unit whose `relations` metadata names a registered relation kind and target, and the page holds no page-level edge of that kind to that target
- **THEN** `suggest_relations` returns a `unit_relation_lift` candidate proposing that authored kind to that target
- **AND** no file under the vault is created, modified, moved, or deleted

#### Scenario: A plain relation bullet is not lifted

- **WHEN** a page expresses a relation as a plain relation bullet inside a block body, which already produces a page-level edge
- **THEN** `suggest_relations` returns no `unit_relation_lift` candidate for that relation

#### Scenario: An already promoted unit relation is not lifted again

- **WHEN** a page carries a typed unit relation and also a page-level edge of the same relation type to the same target
- **THEN** `suggest_relations` returns no `unit_relation_lift` candidate for that relation type and target

#### Scenario: Pages sharing an open question are proposed as related

- **WHEN** two pages each carry a semantic unit that is an open question with the same normalized question text
- **THEN** `suggest_relations` returns a `shared_open_question` candidate from each page to the other

#### Scenario: Pages answering the same target are proposed as related

- **WHEN** two pages each carry a unit-level `answers` or `resolves` edge to the same target
- **THEN** `suggest_relations` returns a `shared_resolution_target` candidate from each page to the other

### Requirement: Question Recall Spans Both Indexed Unit Axes

Structural question matching SHALL select question units from both the unit-kind
axis and the unit-category axis, so that a rich open-question block, a
rich open-question block carrying a category override, and a compact question
observation all participate. The selection SHALL use separately indexed branches
rather than a disjunction across the two columns. Question text SHALL be
normalized identically on both sides of the comparison; that normalization is
ASCII-only case folding plus removal of trailing question marks, and those
limits SHALL be treated as deterministic recall limits rather than defects.

#### Scenario: All three question forms match one another

- **WHEN** one page carries a rich open-question block, a second carries a rich open-question block with a category override, and a third carries a compact question observation, all with the same question text
- **THEN** each page receives a `shared_open_question` candidate naming both of the others

#### Scenario: Case and trailing question marks do not prevent a match

- **WHEN** two pages carry the same ASCII question text differing only in letter case and a trailing question mark
- **THEN** the two pages receive a `shared_open_question` candidate for each other

#### Scenario: A non-ASCII case difference is not normalized away

- **WHEN** two pages carry question text differing only in the case of a non-ASCII letter
- **THEN** no `shared_open_question` candidate is returned for that pair

### Requirement: Structural Suggestions Share One Snapshot And Soft-Fail

All structural generators for one page SHALL read the typed graph sidecar
through a single validated read snapshot. When that snapshot is unavailable,
`suggest_relations` SHALL return zero structural candidates, SHALL still return
its deterministic non-structural candidates, and MUST NOT raise. Each structural
generator SHALL issue a bounded, constant number of sidecar queries independent
of corpus size.

#### Scenario: Unavailable sidecar still yields deterministic suggestions

- **WHEN** the graph read snapshot is unavailable and `suggest_relations` is called for a page carrying wikilinks and typed unit relations
- **THEN** the wikilink candidates are returned, no structural candidate is returned, and no error is raised

#### Scenario: Query count does not grow with the corpus

- **WHEN** `suggest_relations` runs against a two-page corpus and against a corpus of two hundred additional matching pages
- **THEN** the number of sidecar queries issued is the same in both cases

### Requirement: Structural Candidates Aggregate And Are Bounded

Each structural generator SHALL emit at most one candidate per pair of proposed
relation type and target, folding every contributing match into a list inside
that candidate's evidence. Each generator SHALL emit a bounded number of
candidates per page, and each candidate SHALL fold a bounded number of matches
into its evidence. Repeated calls over an unchanged corpus SHALL return
identical candidates.

#### Scenario: Two matches between the same pair yield one candidate

- **WHEN** two pages share two distinct open questions
- **THEN** exactly one `shared_open_question` candidate is returned for that pair, and its evidence lists both questions

#### Scenario: Two authoring units yield one lift candidate

- **WHEN** two semantic units on one page each author the same relation kind to the same target, and no page-level edge of that kind exists
- **THEN** exactly one `unit_relation_lift` candidate is returned, and its evidence lists both authoring units

#### Scenario: A large matching corpus stays bounded

- **WHEN** two hundred pages each carry the same open question as the requested page
- **THEN** at most three `shared_open_question` candidates are returned

#### Scenario: Folded evidence is capped and the total stays honest

- **WHEN** two pages share more open questions than one candidate's evidence may carry
- **THEN** the candidate's evidence lists the capped number of matches and separately reports the true total

#### Scenario: Two co-participation methods do not propose the same edge twice

- **WHEN** two pages both share an open question and both answer the same target, so `shared_open_question` and `shared_resolution_target` would each propose `relates_to` to the same page
- **THEN** exactly one structural candidate is returned for that relation type and target

### Requirement: Structural Evidence Identifies The Driving Unit

A structural candidate whose evidence is derived from a page other than the
candidate's source page SHALL include that other page's semantic unit identity —
its unit reference, its anchor, and the relation kinds it used where applicable.
A dismissed candidate SHALL therefore resurface with a different fingerprint
when the page that produced its evidence changes, even while the source page
remains byte-identical.

#### Scenario: Dismissal expires when the other page changes

- **WHEN** a `shared_open_question` candidate is dismissed and the other page's question unit is then re-anchored while the source page stays byte-identical
- **THEN** the candidate reappears in the relation queue with a different fingerprint

#### Scenario: Lift evidence names its authoring unit

- **WHEN** a `unit_relation_lift` candidate is returned
- **THEN** its evidence names the authoring unit's unit reference and anchor, the authored relation label, and the resolved relation family

### Requirement: Structural Generators Are Ordered Ahead Of Wikilink Suggestions

Structural generators SHALL be invoked before the wikilink, frontmatter-source,
shared-source and embedding-proximity generators, and those four SHALL retain
their existing order relative to one another. Because the response is truncated
at the requested limit, this ordering is a budget: an author-written typed
relation is the highest-evidence signal available and SHALL NOT be displaced by
unbounded body-wikilink candidates, which are the lowest-cost to regenerate on a
later read.

#### Scenario: A link-heavy page still yields its structural candidates

- **WHEN** a page carries more body wikilinks than the requested limit and also carries a typed unit relation with no page-level counterpart
- **THEN** the response still contains the unit-relation-lift candidate

#### Scenario: Candidate order places structural candidates first

- **WHEN** `suggest_relations` returns candidates from every generator for one page
- **THEN** every first structural candidate appears before the first wikilink candidate
- **AND** the first wikilink, frontmatter-source, shared-source and embedding-proximity candidates remain in that relative order

### Requirement: Registry changes rebind derived edges without a Markdown census

The graph synchronizer SHALL make `epoch_writes` recognize the exact protected relation-registry target and internally inject a graph generation floor and full-scope checkpoint around caller-supplied registry YAML. Registry callers MUST NOT append epoch files manually. One full-marker convergence dispatcher shared by live post-guard synchronization and `index_sync` deferred drain SHALL publish a new derived snapshot by re-resolving stored raw observations when the graph registry hash differs and schema, source freshness, instance identity, generation, and prior registry hash proofs hold; otherwise it SHALL run the existing recoverable full rebuild. Before dispatch, it SHALL sample the settled epoch and observed marker generation while holding the canonical mutation boundary, leaving pre-commit debt untouched when that boundary is busy. Rebind SHALL preserve edge keys, endpoints, raw labels, exact review-evidence inputs, source provenance, and unrelated metadata while updating registry-derived fields atomically. Successful rebind or rebuild SHALL acknowledge the checkpoint and compare-and-swap clear only the observed full marker. It MUST NOT parse or rewrite every Markdown page while the registry writer boundary is held. Startup and reconcile SHALL use the same dispatcher for a committed unacknowledged registry epoch even if no worker was registered before a process stop.

#### Scenario: Alias registration activates historical raw edges
- **WHEN** a current source graph contains unregistered raw `applies_to` edges and a registry with alias `applies_to` is committed
- **THEN** a derived rebind changes only the registry-derived fields for those edges and publishes one coherent current snapshot
- **AND** page content, source hashes, edge keys, endpoints, and provenance remain unchanged

#### Scenario: Unsafe rebind falls back visibly
- **WHEN** the previous graph is stale, has an incompatible schema, or does not carry the expected prior registry hash
- **THEN** the system does not publish a partially rebound graph
- **AND** it reports or schedules the existing recoverable full rebuild state

#### Scenario: Pre-commit full marker cannot race the registry batch
- **WHEN** the durable full marker is enqueued before canonical registry bytes are replaced
- **THEN** the convergence dispatcher cannot begin rebind or rebuild until it obtains the canonical mutation boundary and observes a settled epoch
- **AND** a successful publication compare-and-swap clears only the marker generation it covered, so newer graph debt survives

#### Scenario: Registry epoch survives every crash cut
- **WHEN** a caught failure occurs before the canonical commit point, an abrupt stop occurs during ordered floor/registry/checkpoint publication, the completed batch stops before worker registration, or private graph publication is interrupted
- **THEN** the caught failure rolls back registry and epoch, while an abrupt mid-batch newer-floor/older-checkpoint cut is classified recoverable rather than current
- **AND** every durable post-registry cut converges through rebind or full rebuild and acknowledges only a coherent snapshot carrying the committed registry hash

### Requirement: Relation filtering expands aliases, families, and replacements coherently

Every graph-backed relation filter SHALL resolve aliases to canonical identity, include extension descendants when a core family is selected, and expand valid acyclic deprecation chains only from their active terminal survivor to deprecated predecessors. A deprecated-key filter SHALL remain limited to that key's own observations and report both immediate replacement and terminal active survivor without pulling in successor observations. Explain and debug output SHALL preserve existing `matched_via="relation_type"` and `matched_via="parent_relation"`, add `matched_via="replacement"` for predecessor matches, and report alias normalization separately as requested and resolved relation identity while preserving stored raw and canonical fields. Match precedence SHALL be exact relation type, replacement, then parent family. Inverse metadata MUST NOT create an edge that was not authored or deterministically derived by an existing rule.

#### Scenario: Exact and family filters agree on one extension edge
- **WHEN** a stored `vault.applies_to` edge has parent `relates_to`
- **THEN** an exact extension filter and a core-family filter can each return it
- **AND** their match provenance distinguishes exact identity from family roll-up

#### Scenario: Replacement expansion reaches historical keys
- **WHEN** an active extension replaces a deprecated extension
- **THEN** filtering by the active extension can return both sets of observations while filtering by the deprecated key returns only its own
- **AND** replacement expansion does not overwrite their stored identities

### Requirement: Relation review queue assembly is graph-native and bounded

The batched relation-review queue SHALL assemble activation eligibility, coverage, deterministic structural candidates, authored-edge suppression, placeholder suppression, stable source ordering, and bounded source groups from one validated graph snapshot. The graph SHALL persist every versioned input needed to reproduce the exact public evidence object, ref, signal version, and fingerprint for each graph-representable deterministic generator, including raw authored body-wikilink target spelling and an internal occurrence order/location needed to select the same occurrence. Internal parity fields MUST NOT alter the public evidence shape. One queue request MUST NOT walk or parse the Markdown corpus, invoke embedding or model scoring, reconstruct the graph, or acquire the Markdown writer boundary. It SHALL preserve unchanged review identity and dismissal behavior, report coverage and truncation honestly, and leave embedding-proximity discovery available only through the explicit per-page suggestion operation.

#### Scenario: Small queue limit does bounded work at vault scale
- **WHEN** a current graph represents 3,600 eligible synthetic pages and the relation queue requests five source groups
- **THEN** candidate assembly uses one graph snapshot and a bounded number of indexed queries independent of the page count
- **AND** it performs zero full-vault Markdown parses and zero corpus cosine searches

#### Scenario: Concurrent mutations do not serialize the queue read
- **WHEN** mutation requests are active while a relation queue read begins
- **THEN** the queue reads a coherent published snapshot without taking the mutation boundary
- **AND** legitimate mutation-busy outcomes elsewhere do not force the queue into a corpus fallback

#### Scenario: Unavailable graph returns a typed state
- **WHEN** no current validated graph snapshot is available
- **THEN** the queue returns bounded `warming`, `pending`, or `unavailable` state with retry guidance and no candidate groups
- **AND** it does not fall back to a Markdown census or embedding work

#### Scenario: Indexed candidates preserve review fingerprints exactly
- **WHEN** an unchanged synthetic corpus is evaluated by the pre-change deterministic generators and by the graph-native batch
- **THEN** every candidate's public evidence object, stable ref, signal version, fingerprint, ordering, and dismissal visibility are byte-for-byte equivalent
- **AND** a body-wikilink candidate retains its authored target spelling and first-occurrence behavior while its public evidence remains exactly `source_path` plus `target`

### Requirement: Relation review decisions revalidate one hinted source

Each relation queue group and item SHALL carry its source path and source content hash. Accept and triage requests for newly returned items MUST echo that source path as a resolution hint. A hinted decision SHALL rederive and validate candidates only for that current source page, current graph snapshot, review fingerprint, and content hash where required. A hintless legacy request SHALL search only the bounded current queue prefix; if no exact ref is found there, it SHALL return refresh-required rather than widening work or scanning Markdown.

#### Scenario: Hinted acceptance stays page-local
- **WHEN** an agent accepts a current queue item with its ref, fingerprint, source path, page hash, and audit reason
- **THEN** the server rereads at most that source page before the governed edit
- **AND** it refuses drift without examining unrelated Markdown pages

#### Scenario: Legacy ref-only triage is bounded
- **WHEN** an existing client triages a relation ref without a source-path hint
- **THEN** the server resolves it only if it occurs in the bounded current queue prefix, otherwise returning refresh-required
- **AND** it performs no activation census, full-vault candidate generation, Markdown walk, or embedding work

#### Scenario: Hintless ref outside the bounded prefix requires refresh
- **WHEN** a pre-upgrade ref identifies a deterministic candidate outside the current bounded prefix or a retired embedding-proximity candidate and no source hint can resolve it safely
- **THEN** the server returns a stable refresh-required result and preserves any recorded review state
- **AND** it does not scan the corpus, widen the queue, or recompute embeddings to recover the proposal
