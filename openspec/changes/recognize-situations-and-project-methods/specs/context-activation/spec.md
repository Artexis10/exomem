## ADDED Requirements

### Requirement: Task context uses one portable packet section

Maps packets SHALL carry a bounded `task_context` section beside the map and turn slice. Its protocol state SHALL distinguish ready, empty, partial, unavailable and skipped results. It SHALL contain candidates, methods and existing section-level missing reasons. Candidate entries SHALL carry canonical identity, source reference, source version, registry-defined carrier, discovery origins, an honest discovery reason and a read reference. Method entries SHALL carry identity, source reference, complete admitted FULL-source version, admitted attribution, applicability completeness and a read reference. Optional method content SHALL reuse governed units or procedures. Incomplete applicability SHALL require reading the source before use. Current method identity SHALL follow existing unit admission, same-page supersession and typed expiry before category selection. Review-due units SHALL remain current read routes. The selected semantic parent SHALL match the carried whole-source version. The existing next-action producer SHALL add closed states `method_reset` and `task_read`. Method reset SHALL precede stance review advice; admitted task read routes SHALL follow stance review advice and precede empty-slice advice. These states SHALL preserve existing authority and capability checks.

#### Scenario: An ordinary client receives usable method context
- **WHEN** a method is selected through any public activation door
- **THEN** the same identity, version and applicability contract reaches the client
- **AND** a partial projection requires a source read rather than implying that absent exclusions do not exist

#### Scenario: Task context remains auditable without retaining its prose

- **WHEN** final egress releases task candidates or methods in a maps response
- **THEN** the existing private served-context record retains their state, source identity, version and read route without source or prompt text
- **AND** earlier records without task metadata remain readable with an explicit unrecorded task state

### Requirement: Method continuation cannot certify stale behavior

The existing activation token MAY carry optional `method_versions` entries with `ref`, `source_ref` and `source_version`. Combined anchor references and method entries SHALL fit the existing 32-entry ceiling and the token SHALL fit its existing 8,192-character ceiling. Entries SHALL be untrusted read hints, rechecked against the caller's current view, and SHALL NOT establish anchor evidence or authority. Existing thread binding and lifetime rules SHALL remain unchanged. Legacy tokens SHALL mean no carried methods; malformed method entries SHALL NOT invalidate independently valid map continuity.

Before task applicability selection, the system SHALL revalidate every carried method. `task_context.methods.reset` SHALL invalidate held projections when a source changed, retired, disappeared, became withheld or could not be checked. Missing and withheld sources SHALL produce the same generic reset without a reference or revealing reason. Current relevant replacements MAY follow. Restricted FULL versions SHALL cover the complete canonical released body and metadata, including operative exclusions outside selected units. Internal raw-byte guards SHALL remain private. Excerpt-only and lower-release sources SHALL supply no new task or method projection; they SHALL retain ordinary pull surfaces with honest partial coverage. If packet or token bounds prevent complete dependency retention, the system SHALL reset the held set and carry only complete new projections that fit. A client without method continuity SHALL freshly read a method before reusing it.

#### Scenario: Changed exclusions remove a method from discovery
- **WHEN** a carried method's new exclusion makes it irrelevant to the current task
- **THEN** revalidation invalidates its held projection before applicability filtering
- **AND** the packet need not repeat its now-unrelated content

#### Scenario: A dependency cannot fit in continuity
- **WHEN** retaining another method would exceed the token or packet allowance
- **THEN** the response invalidates the prior held set and retains only complete projections that fit
- **AND** it never silently evicts a dependency while certifying the old projection

#### Scenario: An older token carries no method fields
- **WHEN** an activation receives a valid legacy token
- **THEN** map continuity retains its existing behavior and no method is assumed to be held

#### Scenario: Small hook budgets preserve method invalidation

- **WHEN** a method reset, admitted stance lines and next action fit but optional routes do not
- **THEN** the hook renders the reset before method content and preserves stance and next action
- **AND** an undelivered reset does not advance method continuity

### Requirement: Maps activation discovers across knowledge carriers

Maps activation SHALL discover bounded relevant candidates from admitted notes, episode sources, supported intention references, methods and collection descriptions. Eligibility SHALL NOT depend on successful anchor resolution or membership in its neighborhood. Discovery SHALL reuse existing retrieval and graph authorities and preserve canonical identity, source version and origin.

#### Scenario: A site mention connects to a prior intention
- **WHEN** a fresh session mentions a workshop and a pre-existing episode links it to an unresolved calibration appointment
- **THEN** the episode remains eligible even when the turn also names unrelated equipment
- **AND** the first-answer workflow can read the supporting source without the user naming its stored home

#### Scenario: A method belongs to another topic
- **WHEN** a relevant comparison method is stored outside the current subject's anchor neighborhood
- **THEN** method discovery can supply it with applicability evidence and its source reference

#### Scenario: A large structured collection is relevant
- **WHEN** a collection description identifies a source of exact facts
- **THEN** activation supplies its bounded query route without indexing or enumerating all collection rows

#### Scenario: Unit release precedes ranking and caps

- **WHEN** a restricted caller can read a page with both visible and withheld units
- **THEN** only admitted units and uncovered released prose contribute corpus statistics, query-term selection, scores and candidate slots
- **AND** visible material remains discoverable without a raw top-k or title-only prefilter

#### Scenario: Incomplete preparation preserves ordinary context

- **WHEN** the complete restricted source corpus cannot finish within the existing preparation, aggregate corpus or request budget
- **THEN** task discovery reports partial or unavailable coverage, preserves maps and ordinary pulls, and makes no absence claim

#### Scenario: Excerpt-only sources keep their existing pull surfaces

- **WHEN** a source has only L5 excerpt permission or a lower release level
- **THEN** this new task corpus consistently omits it and reports partial discovery without exposing a raw source hash or unadmitted metadata
- **AND** existing canonical read, find and map pulls remain available under their current contracts

#### Scenario: Restricted routes report their limits

- **WHEN** a restricted caller activates task context
- **THEN** task graph expansion reports an omitted lane until origin-unit release can precede its caps
- **AND** collection claims routing requires existing FULL metadata admission, while independent released text remains eligible

### Requirement: Candidate context does not manufacture ongoing intent

The system SHALL distinguish topical contact, recent activity and supported ongoing intent. It SHALL retain declared current state and relevant correction, closure and supersession references. Unknown state SHALL remain unknown. An unfinished episode or recent edit SHALL NOT create a commitment or establish that an earlier plan remains active.

#### Scenario: A recently edited recap describes a cancelled plan
- **WHEN** a recap is recent but linked current evidence records cancellation
- **THEN** the first-answer workflow retains the cancellation and does not recommend continuing that plan

#### Scenario: The user had only considered an option
- **WHEN** a source records an undecided possibility
- **THEN** returned context retains that uncertainty rather than describing a purchase or commitment as settled

### Requirement: Method projections bind the operative source

A method projection SHALL carry identity, attribution, source version and applicability evidence or a route to read it. Its version SHALL cover all content used to apply the method, including exclusions. Missing or truncated applicability SHALL remain explicit. Local adaptations and current user overrides SHALL remain distinct from reusable revisions.

#### Scenario: An exclusion is outside the selected unit
- **WHEN** a selected technique unit stays unchanged but an exclusion elsewhere in its source changes
- **THEN** the prior method projection is stale and cannot be presented as the current complete method

#### Scenario: The user overrides a method for one task
- **WHEN** the user requests a different approach for the current task
- **THEN** the agent can follow that request within existing authority without silently rewriting the reusable method

### Requirement: Method refresh is independent of map freshness

Every activation carrying delivered method versions SHALL revalidate them before discovery and applicability filtering. It SHALL invalidate stale held projections even when the method no longer matches the current task. It SHALL return relevant changed content or an admitted withdrawal or successor indication when appropriate. An unchanged or deliberately stale map SHALL NOT validate a stale method. A withheld method SHALL behave like an absent method without disclosing a prior hidden identity.

#### Scenario: Another client changes the reusable method
- **WHEN** one client commits an authorized method revision and another client next activates relevant context
- **THEN** the second client receives the current method state even if its map generation is unchanged

#### Scenario: A method is withdrawn
- **WHEN** a previously delivered method is explicitly retired within the caller's admitted view
- **THEN** the next activation carrying its version invalidates that held method, even when retirement removed it from discovery

### Requirement: Context layers share bounded selective delivery

Maps, profiles, stance, candidate context and methods SHALL reuse the existing packet budget and continuation contracts. Next-action prose SHALL be reserved first. Stance SHALL compile before the fixed shared task/slice reserve is computed from remaining material space. The common prose counter SHALL charge each section and next action once. Task pinning SHALL bind the full public cap. Overflow SHALL provide bounded read routes or honest partial status. A failed optional stage SHALL preserve available context. Refresh SHALL follow a material task or evidence change rather than every memory reply.

#### Scenario: A crowded packet needs depth
- **WHEN** useful candidate evidence exceeds the packet budget
- **THEN** the packet retains source routes and reports incomplete content without increasing the limit or truncating a claim into a false statement

#### Scenario: Discovery is incomplete
- **WHEN** a bounded search omits a relevant carrier or cannot complete
- **THEN** the result reports that limitation and does not claim the knowledge or its canonical home is absent

## MODIFIED Requirements

### Requirement: Read-only activation operation
The product SHALL expose a read-only operation `activate_context` that accepts a raw
original human turn (`turn`, non-empty text), an optional `max_chars` (default 4000, clamped
to 500..8000), an optional declared `purpose`, an optional `include_timings`
flag, and optional `client` and `session` attribution, and returns a working-memory
packet. It SHALL accept an optional `trigger` with the closed protocol values
`user_turn` and `task_refresh`, defaulting to `user_turn`, and optional descriptive
`initiator` attribution. Declared attribution SHALL NOT establish identity or authority.
The operation SHALL be reachable over the
same leaf function on MCP, the CLI (`exomem activate "<turn>"`, with `--client` and
`--session`) and the personal REST facade (`/api/activate_context`). It SHALL perform
no vault write, SHALL NOT change the
hits, ordering or envelope of `ask_memory`/`find` for any input, and SHALL run no model
other than the retrieval scorers ordinary recall already runs or the explicitly enabled bounded context-fit interpreter.
The context-interpretation capability governs that exception; disabling it SHALL preserve the ordinary agent-only path.

Each call SHALL append one row to a host-local activation log beside the existing query
logs, outside the vault. The row SHALL carry the client label the transport observed or,
failing that, a declared `client` matching a short software-label pattern, a stable
per-vault hash of `session` rather than its value, a hash identifying the vault, the transport, the outcome and
packet-derived counts. It SHALL NOT carry the turn text, and SHALL omit anchor
identifiers when the packet is content-private. An invalid `client` or an oversize
`session` SHALL be recorded as invalid and never refused or echoed. `client` and
`session` SHALL NOT change the packet's material. `session` MAY select the caller whose
session start carries the optional `upkeep` block that the adaptive-memory-maintenance
capability defines, and nothing else. On the MCP door only, while
proactive capture is permitted, the packet MAY carry a one-sentence `episode_due`
advisory after repeated activations from one caller without an episode record, at most
once per bounded interval; it SHALL NOT appear on the CLI or REST doors, nor for a
Claude Code or Codex MCP client whose hook this vault's activation log shows served it
within that interval.

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

#### Scenario: Attribution never changes the packet
- **WHEN** the same turn is submitted against the same vault state with and without
  `client` and `session`, or with an invalid label or oversize session
- **THEN** the packets are identical apart from the continuity token each call mints,
  the optional `upkeep` block, whose session start the `session` identifies, and
  `budget.used_chars`, which counts that block's item when one is attached
- **AND** the call is never refused over its attribution

#### Scenario: The turn is never recorded
- **WHEN** an activation is logged
- **THEN** its row carries neither the turn text nor the raw session value
- **AND** a content-private packet's row carries no anchor identifiers

### Requirement: The maps packet
In `maps` mode, activation SHALL return the map, its generation, the budget report, and the same governance and generation metadata as an `anchors` packet. It SHALL NOT call a model other than retrieval scorers or the explicitly enabled interpreter governed by context-interpretation, or write to the vault. It SHALL NOT resolve anchors, except for the turn slice, which the setting `EXOMEM_ACTIVATION_TURN_SLICE` turns off and which "The turn slice" governs. The map, any attached profile, stance, the turn slice, task context and next-action prose SHALL share the request's `max_chars` cap. Stance allowance SHALL preserve mandatory map framing after the fixed shared reserve. This allowance SHALL remain independent of the turn and held methods. The cap SHALL charge the packet's prose: the map's description line, one rendered line for each map, slice, candidate and method item, all operative method content and invalidation advice, and the overflow line. It SHALL NOT charge structure fields: refs given as fields, paths, profile routes, topics, dates, the generation blocks, the continuity token and timings. The budget report SHALL carry `serialized_chars`, the length of the packet's compact JSON form without that field. A map item SHALL omit `topics` when it is empty and `lifecycle_class` and `state` when they have no value. The map's description SHALL name the read call for a map ref. When the owner's map inputs changed after an earlier map for the same owner was built, activation MAY serve that earlier map marked `stale: true`, with `generation.index_stale: true` and the index generation it was built from, and SHALL then rebuild the map off the request thread. A restricted caller's map SHALL NOT be served from an earlier build. A vault with no context-catalogue entries visible to the caller SHALL return an empty map with the reason `empty`. The `continuity` token SHALL carry the map generation, so that a caller can tell when its map is outdated, and SHALL NOT encode anchors. It MAY carry the bounded method-version hints defined by method continuation; those hints SHALL NOT become anchor continuity evidence. When a request carries a `continuity` token from a `maps` packet whose map generation equals the generation of the map that activation would serve to that caller, the map block SHALL carry its ref, generation, stale flag, `unchanged: true` and a one-line description, and no items. `conversation`, `focus` and `trigger` SHALL NOT change the map, and neither SHALL the turn: the turn, its conversation, its focus and its trigger MAY change the turn slice and task context, while leaving the map independent of those inputs. A `maps` packet SHALL carry the `episode_due` advisory under the same conditions as an `anchors` packet. The call ledger SHALL record the released-item count of a `maps` packet as unknown, not zero, because a map response carries no per-page basis to count.

#### Scenario: An empty vault returns an empty map
- **WHEN** no context-catalogue entry is visible to the caller
- **THEN** the packet carries an empty map with the reason `empty`

#### Scenario: The map fits the budget
- **WHEN** the caller sets `max_chars`
- **THEN** the map and any attached profile together fit within it

#### Scenario: The budget charges prose and reports the serialized size
- **WHEN** activation serves a `maps` packet for `max_chars` 8,000
- **THEN** `budget.used_chars` is at most 8,000 and counts only the prose lines
- **AND** `budget.serialized_chars` equals the length of the packet's compact JSON form without that field

#### Scenario: An unchanged map is not sent again
- **WHEN** a caller passes the `continuity` token of its last `maps` packet and the map it would be served has the same generation
- **THEN** the map block carries `unchanged: true` and no items
- **AND** a call with no token, or with a token of an older generation, receives the full map

#### Scenario: A write does not hold the owner's activation
- **WHEN** the owner's map was built and a vault write then changes its inputs
- **THEN** the next activation serves the earlier map with `stale: true`, `generation.index_stale: true` and the index generation it was built from
- **AND** after the rebuild finishes, an activation serves the new map with `stale: false`

#### Scenario: Conversation does not change the map
- **WHEN** two `maps`-mode calls differ only in `conversation`
- **THEN** both return the same map

#### Scenario: With the setting off, the packet is unchanged
- **WHEN** `EXOMEM_ACTIVATION_TURN_SLICE` is `off`
- **THEN** the `maps` packet carries no `turn_slice` block and preserves the independent map and task-context behavior for the same vault and turn

#### Scenario: The episode advisory rides on a map
- **WHEN** the conditions of the `episode_due` advisory hold and activation serves the `maps` packet
- **THEN** the packet carries `episode_due`
- **AND** nothing else in the packet differs from the same call without the advisory

#### Scenario: The call ledger does not report a map as zero pages
- **WHEN** activation serves the `maps` packet
- **THEN** the call ledger records the released-item count as unknown, not zero

### Requirement: The turn slice
A `maps` packet SHALL carry a `turn_slice` block beside the map, unless `EXOMEM_ACTIVATION_TURN_SLICE` is `off`. The slice is on by default and for any other value of the setting, and the setting has no effect in `anchors` mode.
- The slice SHALL list the leads that the guarded anchors compile selects for the user turn and the request's conversation: resolved and partial anchors, the senses of an ambiguous turn, pages that the turn names by title, related pages and the carried page. It SHALL NOT list `recent_context`. The anchors compile SHALL be one helper that both packet modes call. In `maps` mode it SHALL run with no continuity refs, no `anchor` and no attribution, SHALL read the conversation as "The turn slice follows the conversation" states, and SHALL NOT mint a token, learn or record heat. It MAY skip the material of the anchors that it resolved, which the slice never serves, and SHALL then give the same leads and state as the full compile.
- The block SHALL carry `state`, one of `ready`, `empty`, `skipped` or `unavailable`, a generation that serves only as the guard's record key, a fixed description, the items and, when some do not fit, an overflow count. The description, which only a `ready` block carries, SHALL say that the block holds what the turn and its conversation reached, with the evidence, and that it is not checked for relevance.
- Each item SHALL carry its catalogue entry's kind, title, description and description origin, its ref, the profile ref and the state line for anchors and collections, `match` and `evidence`. `match` SHALL be one of `resolved`, `partial`, `ambiguous`, `named`, `related` or `carried`. `evidence` SHALL be the categorical evidence kinds that the evidence requirement fixes, with no score.
- Display text SHALL come only from catalogue entries that the caller may see. A lead with no such entry SHALL be dropped. A turn with no admitted lead SHALL give `state: empty` and no item.
- The map SHALL compile first, over the caller's view, at the request's `max_chars` less a reserve that one named constant fixes, or with no reserve where no task context is compiled and that share cannot hold one line. A slice that fails or finds no room SHALL set its `state` and SHALL NOT remove or change the map. The slice SHALL use a cooperative deadline one activation stage reserve before the request deadline. The final disclosure guard SHALL still check the built map if a synchronous slice stage overruns that deadline. The slice SHALL NOT change `map_generation` or anchor continuity. Independent method-version hints SHALL follow the method-continuation contract. Slice lines SHALL be whole lines, lines that do not fit SHALL become the overflow count, and `used_chars` SHALL count the map, slice and task context together.
- For a restricted caller, the slice's lexical pass and its dependence check SHALL choose the turn's terms and rank on the pages that the caller admits.
- The terminal guard SHALL serve a slice item only from a record that the invocation pinned for the same caller and packet limit, and SHALL serve an `unavailable` slice otherwise. It SHALL drop a slice that stands beside no map.
- The relevant-due-refs association SHALL read the resolved items of the slice as it reads resolved anchors.
- The retrieve hook SHALL render the slice on a turn whose map generation is unchanged.

#### Scenario: An irrelevant turn gets no slice items
- **WHEN** the turn reaches no lead that the guard admits
- **THEN** `turn_slice.state` is `empty` and the block holds no item

#### Scenario: The slice does not change the map
- **WHEN** two turns, one that resolves an anchor and one that reaches nothing, activate over the same vault
- **THEN** both packets carry the same map block and the same `map_generation`; independent method hints may differ

#### Scenario: A withheld anchor changes no byte for a restricted caller
- **WHEN** a vault holds, beside a visible anchor, a withheld anchor of the same name and a withheld titled page
- **THEN** a restricted caller's packet is byte-identical to the packet from a vault without them

#### Scenario: Withheld pages move no lexical statistic of the slice
- **WHEN** a vault holds withheld pages that share words with the turn
- **THEN** a restricted caller's packet is byte-identical to the packet from a vault without them

#### Scenario: Due advice rides on a resolved lead
- **WHEN** the turn resolves an anchor and a due item bears on that anchor
- **THEN** the packet carries `relevant_due_refs` for it

#### Scenario: A failed slice keeps the map
- **WHEN** the slice cannot be built, or cannot be served
- **THEN** `turn_slice.state` is `unavailable` or `skipped`, and the map block is the one that any other turn gets

#### Scenario: A forged slice is not served
- **WHEN** a slice item was not pinned for this caller and packet limit, or the slice was built for a larger packet
- **THEN** the packet carries an `unavailable` slice and the map is unchanged
