## MODIFIED Requirements

### Requirement: The turn slice
A `maps` packet SHALL carry a `turn_slice` block beside the map, unless `EXOMEM_ACTIVATION_TURN_SLICE` is `off`. The slice is on by default and for any other value of the setting, and the setting has no effect in `anchors` mode.
- The slice SHALL list the leads that the guarded anchors compile selects for the user turn and the request's conversation: resolved and partial anchors, the senses of an ambiguous turn, pages that the turn names by title, related pages and the carried page. It SHALL NOT list `recent_context`. The anchors compile SHALL be one helper that both packet modes call. In `maps` mode it SHALL run with no continuity refs, no `anchor` and no attribution, SHALL read the conversation as "The turn slice follows the conversation" states, and SHALL NOT mint a token, learn or record heat. It MAY skip the material of the anchors that it resolved, which the slice never serves, and SHALL then give the same leads and state as the full compile.
- The block SHALL carry `state`, one of `ready`, `empty`, `skipped` or `unavailable`, a generation that serves only as the guard's record key, a fixed description, the items and, when some do not fit, an overflow count. The description, which only a `ready` block carries, SHALL say that the block holds what the turn and its conversation reached, with the evidence, and that it is not checked for relevance.
- Each item SHALL carry its catalogue entry's kind, title, description and description origin, its ref, the profile ref and the state line for anchors and collections, `match` and `evidence`. `match` SHALL be one of `resolved`, `partial`, `ambiguous`, `named`, `related` or `carried`. `evidence` SHALL be the categorical evidence kinds that the evidence requirement fixes, with no score.
- Display text SHALL come only from catalogue entries that the caller may see. A lead with no such entry SHALL be dropped. A turn with no admitted lead SHALL give `state: empty` and no item, except that a turn whose retrieval carry the request budget cut SHALL give `state: skipped`: the compile abstains `unresolved` with `missing[] {role: "anchors", reason: "budget"}`, which means that the slice did not look, not that the turn names nothing.
- The map SHALL compile first, over the caller's view, at the request's `max_chars` less a reserve that one named constant fixes, or with no reserve where that share cannot hold one line. A slice that fails or finds no room SHALL set its `state` and SHALL NOT remove or change the map. The slice SHALL use a cooperative deadline one activation stage reserve plus the task-context reserve before the activation's deadline, so that task discovery keeps the time it needs after a slow compile. The task-context reserve SHALL be a quarter of the activation's budget, at most five seconds, and none when no budget is bound. The final disclosure guard SHALL still check the built map if a synchronous slice stage overruns that deadline. The slice SHALL NOT change `map_generation` or the `continuity` token. Slice lines SHALL be whole lines, lines that do not fit SHALL become the overflow count, and `used_chars` SHALL count the map and the slice together.
- For a restricted caller, the slice's lexical pass and its dependence check SHALL choose the turn's terms and rank on the pages that the caller admits.
- The terminal guard SHALL serve a slice item only from a record that the invocation pinned for the same caller and packet limit, and SHALL serve an `unavailable` slice otherwise. It SHALL drop a slice that stands beside no map.
- The relevant-due-refs association SHALL read the resolved items of the slice as it reads resolved anchors.
- The retrieve hook SHALL render the slice on a turn whose map generation is unchanged.

#### Scenario: An irrelevant turn gets no slice items
- **WHEN** the turn reaches no lead that the guard admits
- **THEN** `turn_slice.state` is `empty` and the block holds no item

#### Scenario: The slice does not change the map
- **WHEN** two turns, one that resolves an anchor and one that reaches nothing, activate over the same vault
- **THEN** both packets carry the same map block, the same `map_generation` and the same `continuity` token

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

#### Scenario: A budget-cut carry is not an empty turn
- **WHEN** the request budget cannot afford the retrieval carry, so that the compile abstains `unresolved` with no anchor, and time is left for the rest of the call
- **THEN** the compile's abstained packet carries `missing[] {role: "anchors", reason: "budget"}` and no title-named or related page
- **AND** `turn_slice.state` is `skipped`, never `empty`, and the map block is the one that any other turn gets
- **AND** a later call with time to spare compiles its own packet and is not served this one

#### Scenario: A slow slice compile leaves task discovery its reserve
- **WHEN** the slice's compile runs past its deadline inside an activation whose own deadline is still ahead
- **THEN** the slice is `skipped` and the map is served, and task discovery runs on the time left, so that it serves the task leads it finds
