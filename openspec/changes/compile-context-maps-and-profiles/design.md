# Design

## Context

`activate_context` today runs freshness, readiness and a lexical BM25 over anchor pages, then `compile_packet`: it analyses the raw turn, builds categorical anchor evidence, resolves anchors and fills role lanes from their neighbourhood. When nothing resolves, a gated lexical carry over the raw turn may serve one uncontested page; otherwise the turn abstains with no units. Activation also embeds the raw turn against anchor signatures (title, lede, headings) for `vector_band` evidence. It never calls the hybrid recall engine behind `ask_memory`. Client instructions tell agents to pass the user's words verbatim and rely on the packet.

A replay of twelve private field reports, with an answer key written by an independent lane, measured where context is lost. Its run files stay outside this repository, so the figures are not reproducible here. The cases are development evidence; they do not decide a switch. The map arm used about a third fewer memory calls than the query arm (22 against 33).

| Arm | Expected page served or selected | Expected page reached |
|---|---|---|
| Current packet, raw turn | 1 | 2 |
| Hybrid recall, raw turn (top 3 / top 10) | 3 | 3 |
| Hybrid recall, agent-written queries (top 3 / top 10) | 8 | 9 |
| Agent with a 3.7 KB map, pulling by ref and query | 7 | 10 |

The strongest result is an agent that sees what exists and pulls. A server-side selector that matches the raw turn by meaning was not tested.

## Goals

- One-shot context: a fresh session, coding or conversational, answers the user's first message with the context it needs, without the user pointing the agent at material. Autonomous pulls before the first answer are part of one-shot.
- Push little: a bounded map. Give depth through pulls by ref or query.
- Scale without a size cap: the vault grows, the map stays bounded, and cold material stays reachable through domain maps and recall.
- Use the epistemic engine fully: semantic units, roles, currency, supersession, typed relations and the vocabulary registries.
- Do no harm: a turn with no relevant memory gets no misleading material, including from the map.

## Non-goals

- A model call inside the activation request path.
- Replacing `ask_memory` or `read_memory`.
- A new `queries` input on `activate_context` before a workflow or latency benefit is shown; agents query through `ask_memory`.
- A mandatory current-state field on every page.
- A new storage format.

## Shape

1. **Context catalogue.** One searchable set of entries, distinct from the existing anchor catalogue. Its entry kinds are topics, anchors, notes (pages that are none of the other kinds), threads (the newest episode revision per `ep-` key) and collections (Records and Planning). It is a derived sidecar of its own: disposable, machine-local, rebuildable from the vault alone, and never read by recall. Notes exclude the trees that the product's layout declaration names as raw material, schema, governance or archive. Activation and the catalogue read that one declaration. Each entry has a one-line description, its memberships, its source refs and the generation it was built from. D11 fixes where descriptions come from.
2. **Map.** A bounded projection of the context catalogue, labelled as an index of what exists, not as material relevant to the turn: live topics, threads, live or pending anchors and collections, one line each with what it is, its state from the current-state authority with its as-of date and that date's source when known, and a ref. Activation in `maps` mode returns it. It does not depend on the turn.
3. **Domain map.** The projection of one topic's entries, read by ref. It is how cold material stays reachable when it is not on the map.
4. **Profiles, read by ref.**
   - An **anchor profile** is what the role lanes compile for one anchor today when an agent passes `anchor`: units by role with currency and refs, current state from Records first, typed relations, related pages and evidence pointers.
   - A **topic profile** compiles the same lanes over a topic's members: the topic's standing stances, preferences, constraints and conventions, the anchors active now, open loops and recent changes.
   Profiles are compiled per request over the caller's candidate entries; no profile is cached.
5. **First-answer workflow.** The agent reads the map, opens the domain maps and profiles that the turn needs, searches with its own words when the map does not cover the need, then answers and cites refs.
6. **Turn slice (a later question, not a contract).** Profiles selected by meaning from the turn and attached to the map. It may be probed on development cases, but it is not an arm of the switch evaluation. It ships only through a frozen evaluation of its own that shows it beats the map alone.

## Decisions

### D1. Map-assisted pulls are the first-answer workflow

The evidence supports an agent that sees what exists and pulls. Activation in `maps` mode returns the map; client instructions make reading it and pulling before the first answer the supported workflow. The map is the only pushed layer until an arm shows that more push helps.

### D2. One catalogue, many projections

The map and the domain maps are projections of one context catalogue, not separately maintained documents. Profiles are compiled from the existing role lanes, not stored as a second copy of the units. This keeps one source for each fact and one place to invalidate.

### D3. Topic membership is a declared contract

A topic is a domain from the vocabulary registry. Membership comes from three declarations:

- **Page field.** A new page field, `topics`, a value or a list of domain keys. The existing `domain` field cannot serve: it names a source's subject and an experiment's folder (`workflow`, `research`, `ops`), so reading it would put every experiment into a topic by accident.
- **Project key.** The project-key registry (`project-keys.yaml`) gains an optional `domain` field per key, a value or a list. A page's project keys are its `project` and `projects` fields, read the way project anchors read them today. The loader resolves the new field and includes it in the registry's content hash, so a change rebuilds what depends on it.
- **Relation.** An authored edge in the epistemic graph whose relation kind has the registry family `membership` (today `member_of`). The entry joins the topics that the edge's target declares through its own `topics` field or project key. An inferred edge does not count.

A declared value resolves when the domains resolver returns it as a built-in, registered or alias key; a deprecated key resolves to its replacement. Writes that set `topics` use the same resolver and the same policy as source capture: a new value registers, and a near-miss warns, because domains are an open space where near-misses are often distinct words. An unresolved value therefore comes only from an edit made outside Exomem. It gives no membership, and the catalogue counts it as an unresolved declaration, so it is visible instead of guessed. Membership runs from the entry to the topic and is not transitive: an entry never joins a topic that its relation's target reaches only through its own relations. A mention of an anchor does not import the anchor's topics. An episode thread belongs to the topics its revision declares, and is otherwise unfiled; its `about` refs are not membership. Ordinary notes become context-catalogue entries in the topics they declare, so a domain map reaches notes that are not anchors. Membership rules live in the registries; code branches only on the closed attributes they declare.

### D4. Current state has one authority

Map lines and profiles take state from the existing current-state resolver: Records first, then declared state pages (as the pending `add-activation-temporal-currency` change defines them, which lands first), then observations. Units keep their own authored dates; a page's modification date does not become a unit's as-of date. A state line shows the resolver's as-of date together with the source it came from, because the resolver can take a profile page's `updated` field as the as-of date of that page's declared state. An entry with no known state shows none.

### D5. Descriptions are written at write time and invalidated by generation

When a write creates or changes a context-catalogue entry, the writing agent may supply its one-line description, which the write stores in the page's `summary` field. Otherwise the catalogue derives one and marks it as derived (D11). Refresh runs through derived sync with explicit reverse dependencies: old and new memberships, incoming relations, deletion, rename, supersession, registry changes and external edits. Refresh coalesces per entry and never publishes a projection built from an obsolete generation. A projection whose inputs changed and are not yet rebuilt is served marked stale.

### D6. Description coverage is a launch dependency

Every entry has a description before any evaluation of the map: authored, from the registry, or from the page's structural lede. The lede costs nothing and covers every page with a body, so it is the first backfill. A model-made backfill runs only if a development round shows that lede descriptions lose against authored ones. A maintenance agent then writes the descriptions into the derived-description store through a write path; the server never calls a model. That backfill is resumable, records its provenance and cost, and reports coverage. The catalogue reports coverage by description origin, and an entry with no description stays visibly undescribed.

### D7. The caller's view applies before compilation

The catalogue, map, domain maps and profiles are computed over the caller's view: hidden inputs never reach synthesis, selection, ranking, ordering, caps, counts, activity scores, staleness marks or generation tokens. A restricted caller's projections are compiled for that view and bypass any shared cache. Counterfactual twins prove it: adding or changing a withheld entry leaves a caller's map, domain maps, profiles and their ordering unchanged. The egress guard learns the new blocks and fields.

### D8. Refs to derived views resolve through one read contract

`read_memory` resolves `map`, domain-map and profile refs with bounded output, pagination for overflow, and explicit missing and stale states. Activation and every surface return the same shapes.

### D9. One packet budget

The map and any slice share the request's `max_chars` cap. Pulls have their own budget per answer. Overflow becomes bounded, paginated refs, never cut claims.

### D10. The `anchors` mode stays until a frozen evaluation decides

The process setting `EXOMEM_ACTIVATION_PACKET` selects `anchors` (default) or `maps`. Requirements that cover read-only operation, governance, freshness, latency and surface parity bind both modes; anchor resolution and the anchors packet shape bind `anchors` mode only. The default changes only through the frozen evaluation below and the normal release line; the setting is the undo.

### D11. The catalogue contract

- **Store.** The catalogue is a machine-local sidecar under the vault's state directory. It stores per-entry rows and raw declarations only: declared `topics` values, project keys, membership edges and source refs. Topics, memberships, state and every view are computed at read time through the caller's view, so a registry edit or a change to a relation's target needs no invalidation fan-out, and D7 holds by construction. Only the owner's activation map is cached, keyed on the catalogue's state. Every view and profile, and a restricted caller's map, is built per request from the published indexes, so no build is stored for any audience but the owner. The store copies the activation index's pattern: a generation token bumped inside each write transaction, a scoped wipe on a schema-version change, incremental updates by per-source signature, and the activation kill switch. Derived sync gains a catalogue component, with its dependencies declared next to the existing components.
- **Entry.** Ref, kind, title, description, description origin (`authored`, `registry`, `model`, `lede` or `none`), topics, source refs, lifecycle class, activity date and generation.
- **Description.** The first that exists: `authored`, the page's `summary` field (a collection's on its `_collection.md` page); `registry`, a topic's domain registry `description` field; `model`, a model-made description for the page's current content hash; `lede`, the page's structural lede cut to one line; `none`. `summary` is the field episode recaps already write and activation already reads, so the change adds no second description field. `model` and `lede` stay separate origins, so a development round can compare them.
- **Derived-description store.** Built only if D6's model backfill runs. A reserved vault tree with its own internal-state descriptor and owner module, as compiled history has. The shared find and recall walk, `read_memory`, directory listing, page reads and write path checks already refuse reserved paths, so none of them serves the store. The owner admits a stored description only to callers who can see the described page. Each stored description records the content hash it describes, its model and its date.
- **Lifecycle class and activity.** The class is the status registry's `class` attribute of the entry's status, read through the existing lifecycle-status helpers. Activity is the page's `updated` field or, for a thread, the date of its newest revision that the caller can see. Units' authored dates are not in any index row, and deciding their visibility per entry per request would cost too much. A thread's identity is its `ep-` key together with its audience, because episode revisions group that way.
- **Map selection.** The map lists topics that have a visible member, ordered by their newest member activity. It then lists threads, collections and anchors whose class is `live` or `pending`, newest first, until the budget is spent. Its last line names the domain maps for the rest. Entries whose class is `superseded`, `retired` or `abandoned` appear only in domain maps. Ties break by ref, so the order is total. `exomem://map` pages through the whole ordered projection; activation returns its first part within the budget. The share of the budget per kind is tuned on development cases and lives in one named constant with its reason.
- **Line.** One line per entry: kind, title, description, the state with its as-of date and source when known, and the ref.
- **Refs.** `exomem://map`, `exomem://map/topic/<domain key>`, `exomem://map/unfiled`, `exomem://profile/topic/<domain key>` and `exomem://profile/anchor/<percent-encoded anchor ref>`.
- **Read.** `read_memory` on one of these refs returns the ref, the generation, a stale flag and at most 40 items, with an opaque continuation token bound to the view, the caller's view and the generation. A malformed, unknown, expired or differently bound token returns the existing `INVALID_CONTINUATION` refusal. A token bound to the same view and caller but an older generation returns `CONTINUATION_STALE`. A ref with no visible view returns `NOT_FOUND`.

### D12. Shared index collection is an internal producer

The shared working-set index collects owner-derived inputs inside the existing `owner_local_producer` scope, limited to collection in its write path. This prevents maintenance from owning a caller disclosure collector and appending governance events during an otherwise read-only startup. The scope restores the principal and collector on success and failure. It does not replace an active operation's authorization or broaden an external caller's authority. Catalogue publication and every external serve retain their existing boundaries. Caller-view filtering, withheld counts and historical admission remain authoritative.

## Evaluation

This protocol is frozen before any held-out generation. A later change is a numbered amendment that says what changed and why. An amendment made after held-out generation starts cannot change the decision rule; if it does, the run is diagnostic only.

### Decision and arms

- **Decision.** Change the default of `EXOMEM_ACTIVATION_PACKET` from `anchors` to `maps`, or keep `anchors`.
- **Arms.** Three arms. For each arm, the full text the actor sees is frozen with a hash: the server instructions, the tool descriptions, the client instructions and the hook output format. The server instructions and tool descriptions are the shipped text and are the same for every arm, because they feed the surface digest; the arms differ only in their client instructions, their push and their tool filter.

  | Arm | Server mode | Pushed at each user turn | Tools | Client instruction lines |
  |---|---|---|---|---|
  | A | `anchors` | what the deployed retrieve hook pushes | Exomem read tools | the current client instructions |
  | M | `maps` | what the deployed retrieve hook pushes | Exomem read tools | read the map and pull before the answer |
  | P | `anchors` | nothing | Exomem read tools without `recall_context` | pull before the answer |

  The Exomem read tools are `recall_context`, `search_memory`, `read_memory` and `browse_memory`. The harness resolves discovery names from the product registry by each command's stable `Command.name`; it does not maintain a second name mapping. The deployed retrieve hook runs in `mcp` mode for A and M: at each user turn it pushes a reminder to activate, and the actor fetches its packet with its own `recall_context` call. That call counts against the pull budget and in Exomem calls, in A and M alike. A is the deployed configuration, so M − A changes both the packet and the instructions. P isolates the map: it shares M's instructions and has no push. P lacks `recall_context` through a client-side tool filter while the shared server instructions still mention it; the report names both differences as confounds. The turn slice is not an arm here; it needs its own frozen evaluation.
- **Actor.** One pinned Claude Sonnet model ID and one pinned client version, headless, on the subscription with no API key in the environment. It has no web, file or shell tools. Its instructions tell it not to name its tools in the answer.
- **Session.** The actor plays a case's quoted user turns in order in one fresh session. The scored answer is the answer to the turn that the key marks; for a single-turn case it is the first answer. No arm receives refs, `focus` or state from outside the session. The push at each user turn does not count against the pull budget.
- **Pull budget.** At most 8 Exomem calls per user turn. A call over the budget is refused, and the actor answers. The hook's reminder is not a call; the actor's `recall_context` call is. A and M therefore have 7 calls left after activating and P has 8; the report names this as a confound of the deployed configuration.
- **Order.** The arm order rotates per case, and sessions of different arms interleave.

### Cases and key

- **Miss cases.** Field reports of activation misses, turned into cases under the corpus split rule. The development split is for iteration. The held-out split is fixed at freeze as a hashed list of case ids and stays unread while iterating.
- **Ordinary turns.** First typed user turns of interactive sessions, sampled at random from the operator's local session records over a fixed window before freeze, without regard to any arm's outcome. The frame is structural; it reads client metadata, the clients' wrapper formats and file-path shapes, never the turn's meaning:
  - Only interactive, non-subagent sessions count: Claude Code sessions whose entrypoint is the interactive CLI and that are not sidechains, and Codex sessions from the TUI started from the CLI or the editor. Headless and exec sessions do not count.
  - Client-work homes are outside the frame, to keep engagement data out of the evaluation.
  - A session counts only when its first prompt came from the operator. A session is excluded when Claude Code's origin fields (`promptSource`, `origin`, `turnOrigin`) or a wrapper format mark its first prompt as not the operator's. The origin values are the client's observed enum, and any value other than an operator prompt excludes.
  - A session that opens with an injected turn is excluded: an agent message in the format of the operator's agent-messaging tools, a task notification or hook feedback. A session that opens with a compaction summary is excluded, and a Claude Code session must come from an external user.
  - The first typed turn skips command wrappers, local-command input and output, system reminders, skill bodies, interruption markers, and Codex's injected instruction and environment blocks. A turn that is only pasted content is the operator's own turn.
  - A session is excluded when its first typed turn carries an image, because the replay supplies text only.
  - A session is excluded when its first typed turn contains a path into the handoff store or a task-brief directory, the bare file name of a document in either machine's handoff store, or the handoff skill's invocation token. Such a turn hands its substance to a file the actor cannot open. This removes resume turns, which are memory-heavy.
  - The report names the bias of each exclusion (resume turns, injected openings, image turns) and the share of candidate sessions that each exclusion removes.
  - Duplicate turn texts are dropped, the pool is shuffled with seed 20261022, and it splits into development and held-out halves of 24 with the rest as a reserve in fixed order. Reserve turns are drawn in that order to replace a held-out turn that the iterating session has read, and to grow a split for the power step.
  - No turn that the iterating session has read enters the held-out split, and the freeze commit records that intersection as zero.
  - No held-out turn has the same normalised text as a turn the iterating session has read, or shares a group link with a read row or a development case. Such a turn is replaced from the reserve like a read turn, and the freeze commit records both intersections as zero.
  - A correction to the frame applies by session id. It drops the rows that the corrected frame excludes, refreshes a first turn that changed, re-applies the text dedupe across all splits, and moves no row between splits. Reserve turns then refill each split to its size before the correction, held-out first. Sessions that only the corrected frame admits get no row; the report names that gap.
  - The held-out ordinary turns form one set across rules 1 to 4, so a reserve turn added for any rule enters all of them, with its key and vault steps 2 to 6.
  - They are keyed like miss cases, and some expect no memory. They guard against harm, because miss cases were selected for A's failure.
- **Synthetic cases.** The deterministic activation audit's positive cases and negative twins, on its synthetic vault, through the agent-in-the-loop arm harness. Gold maps to required facts, and poison maps to forbidden claims. The harness implements A, M and P, and a dry run passes before freeze.
- **No-memory cases.** Held-out ordinary turns and held-out miss cases whose key expects no memory, and the negative twins.
- **Groups.** The key writer fixes the groups before generation: cases from one incident thread form one group, ordinary turns whose sessions share an episode thread key under any scheme form one group, and each negative twin joins its positive. Two cases also share a group when provenance links them, directly or through sessions that are not cases: one case's session hands off to the other's, or one case's incident record cites the other's. The power step uses the frozen groups and their sizes, the freeze commit records the group count per rule, and a rule with fewer than 6 groups reads as inconclusive.
- **Key.** An independent key writer keys every field case against the snapshot only, with refs into it, at the snapshot's time. For each case it records the required current facts and constraints with their source refs, the forbidden claims, the accepted successors of each required fact, and the excluded refs (pages that record the miss itself). Episode provenance uses every key scheme the clients use: the capture hook's key under the client label the hook hashes, the older key built from the session id, any thread key that the session's own revisions carry, and the key that the session's own transcript shows in its episode calls. A scored turn that asks about the system's own behaviour is keyed on its domain content only. The measured session never writes or reads the held-out key.

### Vault and state

1. Take one digest-pinned snapshot copy of the vault. The key writer writes the key on this copy.
2. Remove the incident items (not their collection's definition page), the evaluation pages and every excluded ref from the copy. The evaluation pages include the iterating session's own episode thread and every audit or replay record of a case. Removals apply to the one shared copy, so a page removed for one case is gone for every case, and no key cites a page that another case excludes. Removal by provenance follows these rules:
   - A linked session is the miss session or the sampled session itself, or any later session that cites the incident record, the field report, or one of that session's episode keys under any scheme. Links are read from provenance, never from wording. A session also links when one of its own writes references the record or key, or when its transcript shows a read call (`ask_memory`, `read_memory` or `browse_memory`) whose arguments or result name the record, the field report or a linked episode key and it wrote afterwards; only its writes after that read are removed. A ref that a push or an `activate_context` result surfaced does not link, because the session did not seek it. A session with no transcript links only through its own writes, and the report counts the sessions that read the record without a later write.
   - From every page that a linked session edited, remove the lines it wrote, including `summary`, lede and `topics` fields. A line is removed when any of these sources attributes it to a linked session:
     1. the page's revision history;
     2. the transcripts of the linked session and of every subagent or lane it spawned: their Exomem write calls and their file edits on vault paths;
     3. only for a linked session with no transcript on the operator's machines, such as a web-client session: every write-log entry in the session's active span that no transcript attributes to another session. The span runs from the session's first to its last recorded activity; when the records give no times, it is the local day of the session's earliest provenance record and the day after.
   - A page that source 3 names counts as edited by that session. Its dated units in the span are removed, and if its other lines cannot be attributed, the page is removed.
   - Subject or topic similarity is never a basis for attribution.
   - If a page's lines cannot be attributed by these sources, remove the page.
   - Registry and schema entries that a linked session added keep their key and closed attributes, because other pages depend on them; description text that a linked session wrote in them is removed. A case whose required item is such an entry is struck.
   - After both splits' removals, the copy builder sets the `updated` value of every page that a linked session edited to the newest of: the newest revision that no linked session wrote; the newest remaining dated unit; the page's `updated` value before the first linked edit, from revision history or a transcript. Only when none of these exists does the page take its `created` value. A linked session's edit then does not move the page's activity.
   - A struck case's linked writes are removed like any other case's, because the copy is shared.
   - Revision history attributes at line level: diffing a revision's prior text against the next state gives the lines that one write changed, so a write that source 3 assigns removes only those lines. A revision matches a transcript write by its hash chain where the hashes share a scheme, and otherwise by time within a fixed tolerance and by the changed lines. When more than one session fits, the revision counts as linked if any session that fits is linked. A line that descends from a linked write by a later edit is removed too.
   - The copy builder computes every restored `updated` value before it changes anything else. It then empties, for every arm, the retained-history trees, the write log (`log.md` and its rotated copies), the index's recent-activity section and the trash trees, because each holds text that a linked write produced. Key writing reads the full snapshot, so attribution keeps these sources. The report names the emptied history, write log, recent-activity section and trash as differences from production, and the harness dry run confirms that a history read returns empty without an error.
   - The report counts the pages that source 3 removed whole because their writes predate retained history.
   - The report counts removed lines per source and per linked session, and pages per `updated` basis with the days each page moved.
   - Every page and episode revision that a linked session created is an excluded ref.
   - A case whose required item exists only in removed lines or removed pages is struck.
   - A later edit that restates an answer without any provenance link escapes this step, and the step-3 sweep catches only verbatim text. The report names that residual leak as a limitation.
3. Search the copy for every case id and every quoted turn string, and record zero hits per case. A page with a hit is removed or its case is struck before freeze. A hit on template text does not count: text in the declared templates, or text that also appears in at least three pages created before the case's session, cannot record the outcome.
4. Check that each required item's text is still present at its source refs in the copy after all removals, including the emptied trees and logs. A case whose required text was removed is struck before freeze.
5. Run the description coverage pass (D6) on the copy.
6. Build state once per tree from the copy and record its digest. Each arm receives a copy of machine state. Each session uses that fresh machine state and the returned original vault. No arm's state carries activity history from the operator's state: no heat ledger, recent-context or usage data.

### Measures and judging

- **Primary measure.** The scored answer uses every required fact and constraint, and asserts no forbidden claim as current. A newer fact passes for a required item only when the key lists it as an accepted successor. A different page that states the same required fact counts. A case's score is the mean over its repeats.
- **Secondary measures.** Expected material reached before the answer; off-target share of pushed and pulled refs; Exomem calls, total input tokens including pushed bytes, and wall time per case; failures by kind; per arm, the share of user turns with a `recall_context` call, because M's map reaches the actor only when it obeys the reminder. The report names the tilt in call counts: M's count includes the `recall_context` call that fetches its map, and P has no such call. Complete-turn and partially quoted cases are reported separately.
- **Contamination.** On no-memory cases, the answer asserts user-specific facts that the turns do not supply. An arm contaminates a case when the judge finds contamination in a majority of the case's repeats.
- **Judge.** The newest Codex `-sol` model at its highest effort, on the Codex subscription, because the actor is a Claude model. The judge is chosen before judging starts, and one judge judges every item. If that route has no quota, a fresh Opus lane judges, and the report names the same-family limitation. For every arm the judge sees the same view: the user turns, the key's required items, forbidden claims and successors, and the answer under an opaque shuffled id, with `exomem://` refs replaced by opaque tokens and every Exomem tool name replaced by one neutral token. The name list is the union of the names in every arm's frozen tool list, in bare and client-prefixed forms (for example `mcp__<server>__<tool>`). Any other description of a call or of the map is a named limitation, measured by the arm-guess audit. It never sees the arm key. The judge is told that the key is current at the snapshot, so an answer that corrects the user's framing with snapshot-current facts is not penalised. It answers through a structured-output schema:
  - each required item: used, missing or contradicted;
  - each forbidden claim: asserted as current, mentioned as past, or absent;
  - on a no-memory case: whether the answer asserts user-specific facts that the turns do not supply, with the span.
- **Judge noise.** The judge re-judges a random fifth of the items. The report gives agreement per verdict kind. Agreement below 0.8 on required-item verdicts triggers the recovery path: a second judge from the other model family re-judges every item blind, with the re-judged items mixed in. An item's verdict stands where both judges agree. Where they disagree, the verdict that weighs against the switch stands. A script applies this tie-break after unblinding; no judge lane does it.
- **Arm-guess audit.** After judging, the judge-noise recovery path and the tie-break, a separate Opus lane reads exactly the judge view of every item and never sees verdicts, keys or the arm key. Through a structured-output schema it gives an arm guess (A, M or P), its confidence, the evidence span and the evidence kind: the answer describes tools or the retrieval process, the answer's content, or no basis. The report gives guess accuracy against 1/3 with a bootstrap interval clustered by group, and a per-arm confusion table; raw accuracy decides nothing, because real treatment effects also change an answer. Every item guessed right on evidence that describes tools or the retrieval process is re-judged blind with the flagged span removed, mixed in with re-judged control items. The same judge route re-judges both sets. Each rule is then recomputed with the re-judged verdicts in place of the originals on the flagged items, and the control items give the noise baseline. If any rule's verdict flips, that rule reads as inconclusive and the default stays `anchors`.

### Runs and failures

- **Repeats.** Three per case per arm.
- **Timeouts.** 300 seconds per user turn.
- **Harness faults.** A rate limit, a transport error or a host fault is a harness fault. A crash of the Exomem server under test is not: it is an actor failure. The session is retried with the same inputs up to twice, and every attempt is logged. A case that still faults is struck for every arm. More than two struck cases void the run.
- **Actor failures.** An actor that gives no answer within the turn limit, exceeds the timeout, or loses its Exomem server to a crash scores 0, and the case stays in the denominator.
- **Usage.** Before the run, measure two cases end to end and report the projected usage. The actor and the judge run on subscriptions, so the run has no metered spend. When a quota runs out, the run pauses and resumes with unchanged inputs.

### Statistic and decision rule

- **Test.** For each pair of arms, the per-case score difference, averaged over cases. Within a group, case differences sum, so the statistic stays the mean over cases. Superiority is a one-sided sign-flip permutation test over groups at 0.025. Non-inferiority within a tolerance δ uses the same test on the difference plus δ. The report also gives a 95% interval from inverting the test.
- **Power.**
  - Rule 1 is pooled: M must beat A on the held-out cases that expect memory, that is, the miss cases in their groups plus the ordinary turns that expect memory, in their groups. The miss stratum was selected for A's failure, so it inflates M − A; the report gives each stratum separately, with its share of the pool. The held-out miss stratum is too small for a one-sided sign-flip test at 0.025 on its own, which is why rule 1 pools; the freeze commit records its size. The power step below sizes rule 1 as it sizes rules 2 and 3: simulate each stratum at its own M − A measured on development data, capped at +0.5, with the measured spread, record both effects, and grow the ordinary stratum from the reserve first. Development evidence puts M − A above +0.5 at retrieval level (+0.67) and M − P close to zero.
  - Rules 2 and 3: the largest acceptable loss is policy, fixed now: δ is at most 0.15 for both rules. Before freeze, on the build that will be frozen, measure the per-case spread between repeats of all three arms on the development ordinary turns and miss cases. Simulate equal arms with that spread: M and A on held-out ordinary turns for rule 2, and M and P on held-out miss cases and ordinary turns pooled for rule 3. For each rule, raise the held-out size first, and raise δ above 0.10, up to 0.15, only when the available turns cannot reach an 80% pass rate for equal arms. Rule 1 has no δ: only its size grows. The power step also simulates rule 3's calls test with the development call counts and records its pass rate. The freeze commit records the spread, the sizes, each rule's δ and the simulated pass rates. If a rule is still short of 80% at the cap with every available turn, the sampling window widens once, by its own length, toward the past, before freeze. Only sessions that start in the added span and pass the frame are drawn; they are deduplicated against every existing row, shuffled with seed 20261023 and appended to the reserve, and no existing row moves. The new turns go through the key and vault steps 2 to 6, the state digests are rebuilt, and the freeze commit records the reserve size before and after. If the rule is still short, the run goes ahead, the freeze commit records its simulated pass rate, and a failure of that rule reads as inconclusive, not as harm; the default stays `anchors`.
- **Rule.** Switch only when all of these hold:
  1. On held-out cases that expect memory (miss cases and ordinary turns), M beats A.
  2. On held-out ordinary turns, M is non-inferior to A within rule 2's δ, as the power step sets it.
  3. M pays over P on held-out miss cases and ordinary turns together. Either M beats P on the primary measure, or both of these hold: M is non-inferior to P within rule 3's δ, as the power step sets it, and M beats P on Exomem calls per case under the same test. M's calls include the `recall_context` call that fetches its map.
  4. On no-memory cases, M contaminates no more cases than A does, plus at most one.
  5. M loses no synthetic positive case that A passes in a majority of its repeats. M loses a case when it fails that case in a majority of its own repeats.
- **Expected outcome.** Because M pays one `recall_context` call per user turn that P does not, rule 3 most likely passes, if at all, through the primary measure. If P beats A and M does not pay over P, the default stays `anchors`, and the report proposes a follow-up change for pull-first instructions without the map.
- **Chain checks.** Before judging, check the snapshot digest and the removals (recounted per source and per linked session, with every restored `updated` value), each arm's captured inputs against its frozen hashes, every case present in the outputs with failures named, and the judge inputs against the captured inputs. After judging, check the unblinding key against the item ids and recompute the scores from the raw verdicts.

### Amendments

1. **Amendment 1 (2026-10-22, before any held-out generation).** Keying both splits exposed these faults, and this amendment fixes them:
   - Most sampled first turns were client injections or handoff receipts, not typed opening turns. The ordinary frame is now structural and stricter, the ordinary sample was redrawn with the same seed, and turns the iterating session had read were replaced from the reserve.
   - Miss sessions wrote their answers into the pages the key needs. Step 2 now removes lines by provenance from every page a linked session edited, and step 4 checks the required text, not only its ref.
   - Hook-derived episode keys missed other key schemes, so provenance covers all of them.
   - Only 5 keyed held-out miss groups remained, below the 6 that a one-sided sign-flip test at 0.025 needs. Rule 1 now pools the miss cases with the ordinary turns that expect memory. This changes the decision rule, before any held-out generation.
   - Removals act on one shared copy, and template text produced spurious sweep hits, so both are now stated, with a time-based template rule.
2. **Amendment 2 (2026-10-22, before any held-out generation).** Keying under amendment 1 exposed these faults, and this amendment fixes them:
   - The prefix frame still admitted client injections: task notifications, agent hand-backs, local-command wrappers and image turns. The frame now reads the client's origin fields and excludes sessions that open with an injected turn or an image. The correction applied by session id: held-out dropped 11 rows, refreshed 1 and refilled 11; development dropped 8, refreshed 2 and refilled 8.
   - The vault's write log names no session, so a key writer attributed lines by date and subject. Step 2 now removes a line when any named source attributes it: revision history, the transcripts of the session and its subagents and lanes, or, for a session with no transcript, its active span. Subject similarity is never a basis.
   - A linked session added a schema entry that later pages use. Registry and schema entries keep their key; text a linked session wrote in them is removed.
   - The held-out guarantee checked ids only. It now also covers normalised text and group links, and a frame correction re-applies the dedupe.
   - The frame correction left the reserve nearly empty, so window widening is now likely. Widening now draws only from the added span and appends to the reserve, so no row moves.
   - The keyed markdown mirror had omitted the vault's retained revision history, so source 3 removed whole pages and struck 11 of 12 held-out miss cases. Revision history now attributes source-3 writes at line level, and the copy drops the history trees. The copy also empties the write log, the recent-activity section and the trash, which carried linked writes, and a session that read the record and then wrote is linked.
   - The capture hook's key matched few Codex sessions, so provenance also reads the key from the session's own transcript.
   - A linked session's edit moved a page's `updated` field, which orders the map. The copy builder now restores it from the newest unlinked revision, the newest remaining unit or the value before the first linked edit, and falls back to `created` only when none exists.
   - Two links grouped cases without a written rule: a handoff between the cases' sessions, and an incident record that cites another case's record. Both now group cases, and a struck case's linked writes are still removed.
3. **Amendment 3 (2026-10-23, before any held-out generation).** The harness review exposed these faults, and this amendment fixes them:
   - The protocol said M's map arrives by push at no call. The deployed retrieve hook runs in `mcp` mode, which pushes only a reminder; A and M both fetch their packet with a counted `activate_context` call. The arms table and the call-count tilt now say so. Rule 3's call comparison therefore charges M for that call, which P never makes.
   - Answers can name tools or describe their calls despite the actor's instruction, which tells the judge the arm. The judge view now replaces every Exomem tool name, from the union of every arm's frozen tool list, with one neutral token; a per-session list would leave P's mentions of `activate_context` visible. An arm-guess audit measures the remaining leakage, and a re-judge without the flagged spans makes a rule inconclusive if its verdict flips.
   - Rule 3 could be read as requiring fewer calls on both paths, which the counted `activate_context` call would make nearly unreachable. It now reads: M beats P on the primary measure, or M is non-inferior to P and makes fewer calls. The expected outcome, the pull budget, the secondary measures and the power step now follow from the counted call.


4. **Amendment 4 (2026-10-23, before any held-out generation).** A development round exposed restored-state warm-up as a setup fault:
   - Copying the built vault preserved modification times but changed file inode status-change times, so inherited catalog signatures failed their freshness proof. Sessions now restore the built vault by a same-filesystem rename at the build's absolute path, matching an unchanged production restart. Machine state remains a checked copy because the server writes it.
   - One original vault and one machine-state baseline belong to each tree. All arms share that original vault and receive identical copies of the baseline, with separate surface captures. A custody receipt identifies the arm, tree, session, content digest and stat-only signature digest before the move. The receipt is written atomically. Every exit returns the vault after checking its content and signatures, including exceptions and interrupts. The next preparation recovers an unchanged vault left by a crashed runner. A changed vault stops the run and stays in the slot with its receipt for inspection; preparation cannot wipe it or return it as pristine. The error names the failed run to discard after inspection and the state to rebuild. Cross-device moves refuse instead of copying.
   - The builder completes vector publication once per tree through the product's recall migration entry point. Each arm carries the same embedding sidecar and active pointer under its state digest. Publication duration is recorded separately from total build time. A skipped or incomplete publication refuses the build.
   - Before final publication, the builder converges media through complete product reconciliation passes and drains admitted extraction work until a pass changes no file. It records passes and files written. The builder then publishes current projections and vectors and verifies coverage of the final files, as amendment 8 specifies. Surface capture follows publication. Any content change from surface capture requires fresh publication and proof or refuses the build. The builder then records the vault content digest and file signature manifest. A fresh server must leave both unchanged before the state is usable. The proof uses the session tap and gate, observes completion of product startup, drains its admitted media queue, and stops the server before comparison. A change refuses the build and reports changed-file counts by extension. This proves startup quiescence, not the absence of future repair or engine-wait transitions.
   - The tap keeps its readiness gate as a safety net. In `mcp` hook mode, it waits for an admitted `ask_memory` probe and clearance of `recall_projection` from `warming.components`. Activation reads that projection, so admission alone can favor the pull-only arm. Hidden probes consume no actor pull budget. The gate records its wait, remaining components and `since_s`; score output tallies each arm's final sessions by remaining component combinations and names missing observations.
   - Setup wait is excluded from actor wall time and the 300-second turn timeout, and usage reports and projects it separately. Gate timeout or server exit during the gate is a harness fault. These faults retry unchanged inputs twice; exhaustion stops the run. A crash after the gate remains an actor failure. A structured warming refusal after the gate is a harness fault. The configurable default gate bound remains 1200 seconds.
   - Earlier probes refused recall for about 290 seconds and first observed `recall_projection` clear between 447 and 608 seconds; the recorded probes used 30-second polling or one-second retry pauses. Embeddings still warmed after 25 minutes because the builder had never published vectors. These measurements motivate the restore and build corrections; they do not establish corrected fresh-server timings. A directory move preserved every measured file signature and admitted the catalog in 6.39 seconds in a fresh process, without a full stdio server.
   - Full fresh-server gate time, remaining warming components, unchanged vault digest after a real session, build time once per tree, media convergence and startup quiescence still require replay proof. Admission does not mean all components are warm: optional components may remain and must be reported. The gate covers `mcp` hook mode only; the `working-set` hook starts its own CLI process and needs separate readiness proof. Same-filesystem placement is required.
5. **Amendment 5 (2026-10-23, before any held-out generation).** At the product's default of 2 encoder threads, an interrupted replay-vault build encoded 49,514 vector chunks for 4,187 of its 15,124 files in 3 hours:
   - The state builder accepts a CPU thread budget for the build child only. It refuses a value above the CPUs that the process may use. Arm hashes, sessions, surface capture, the quiescence proof and the judge never see the budget. It is not an arm input, so it varies nothing between the arms of one tree.
   - The state record and the score record carry the budget, or null when the product default applied, and whether the build resumed. All arms of a tree share its embeddings and media output. Thread count can change floating-point reduction order, so a comparison across rounds built with different budgets names both budgets.

6. **Amendment 6 (2026-10-23, before any held-out generation).** The product renamed its advertised activation and search tools:
   - The harness resolves the four read commands through the product registry by `Command.name`. Tool filters and activation counts use `Command.public_name`.
   - The discovery names are now `recall_context` and `search_memory`. The execution identities, read capabilities, pull budget and decision rules stay the same.
   - Development rounds from earlier builds keep their pinned surfaces. A development round on the renamed surface must pass before the freeze.

7. **Amendment 7 (2026-10-23, before any held-out generation).** Reuse a completed tree across serial rounds at its original resolved serving path:
   - Preserve the original absolute `slot/vault` path. Machine-state identity hashes that path; unchanged vault bytes and signatures alone do not establish compatibility.
   - After the donor round finishes and returns custody, archive its complete evidence separately. Retain build metadata and baseline machine state for independent recomputation.
   - Prepare the successor at the original run path. Copy baseline machine state and move the original vault on the same filesystem, preserving signatures.
   - Verify snapshot, retrieval profile, baseline digests, vector publication, quiescence, and product/index compatibility before reuse. Preserve the original CPU budget and resume fields.
   - If index code changes, seed the original slot with the donor's complete machine state and rerun product publication before creating successor proof. Retain accepted vectors only when their fingerprint and coverage remain compatible; report encoding activity.
   - Keep donor provenance and vector-build CPU budget distinct from successor preparation. Record custody before moving the seeded vault; an interrupted preparation preserves the seed and cannot fall through to a destructive fresh build.
   - Preserve missing legacy build settings as unrecorded. An explicit null CPU budget retains its meaning of the product default; missing provenance does not establish that default.
   - Capture every successor arm's surface anew. Record donor provenance, verify donor score recomputation, and report the successor's readiness and unchanged-vault proof.
   - Retain the donor's pinned scorer. It reads the original vault's page inventory; later recomputation temporarily returns that unchanged vault to the archive while both rounds are idle, then restores it to the successor. Do not substitute another scorer, rewrite stage digests, or create placeholder files.
   - Donor and successor work never overlap. Reuse does not change cases, keys, removals, arm budgets, or the one blind held-out decision.

8. **Amendment 8 (2026-10-24, before any held-out generation).** Recover incomplete preparation without treating a failed checkpoint as a completed tree:
   - Preserve the stopped failed attempt in an archive. Prepare a successor at the original absolute serving path through an explicit import mode on `prepare` and a distinct checkpoint option on `state`.
   - Copy the case and plan files as opaque, unchanged bytes. Preserve experimental inputs. Record predecessor identity, the successor harness pin and the product source pin separately.
   - The builder reports its observed product identity. Before surface capture or completed-state publication, compare it with the checkpoint's expected identity. Store the observed identity in the finished provenance; sharing an interpreter does not prove identical imports.
   - Before copying, compare the recorded protocol and retrieval settings with the current implementation. Refuse incompatible inputs instead of executing new settings under old metadata.
   - A registry-proven discovery rename remains compatible when every affected tool keeps the same execution identity and order. Record predecessor and current discovery projections explicitly. Execute and report the current projection; keep every other protocol field unchanged.
   - Import only retained baseline machine state. Copy the vault again from the frozen source. Never reuse the failed attempt's dirty vault, rewrite its custody receipt or manufacture a completed state record.
   - Record a versioned successor intent before copying. Check it before custody recovery or any destructive ordinary preparation. Missing or mismatched checkpoint selection refuses without changing the archive or partial successor.
   - An interrupted import restarts from the immutable archive. Reject equal, nested or symlink-aliased archive and successor roots. Reject a frozen source inside either root.
   - Record the checkpoint digest as its observed identity, not as an earlier certified digest. Preserve failure evidence, publication output, original slot identity and source pins. Missing legacy settings stay unrecorded.
   - Converge media before final publication. Reuse the existing product publication, incremental reconciliation and per-parent reconstruction checks. Do not add a separate product migration API.
   - Reconcile the complete file census and prune stale vector rows before checking each final Markdown parent's exact text and semantic coverage. Reconstruct existing publication through the product's exact stored-row checks. When reconstruction is unavailable, use the product's writer and require completed publication for that one eligible parent with unchanged source and signatures.
   - Report reconstructed parents separately from freshly published parents. A fresh local publication can complete without the service's optional proof object. Requiring reconstruction again would reject supported timed media or repeat segmentation inference.
   - An accepted active pointer alone does not prove coverage. Same modification times do not establish unchanged text. A final content change invalidates the coverage proof.
   - Carry the content digest established by final coverage through the builder result. Check it after catalogue work and in the parent before surface capture and freezing.
   - Report retained vectors, encoding activity, media convergence, publication compatibility and successor preparation settings separately from predecessor build settings. Timed media can require inference; ordinary provider, privacy and cost limits still apply.
   - Earn a fresh surface capture, content digest, signature manifest and startup quiescence proof before creating a usable state record. Keep stage validation and the proven-donor contract in amendment 7 unchanged.
   - No arm generation or scoring follows a failed quiescence check. Recovery changes preparation only, never cases, keys, removals, arm budgets or the blind held-out decision.

9. **Amendment 9 (2026-10-24, before any held-out generation).** Audited reads append disclosure receipts inside a governed vault:
   - The proof's readiness probe and the session actors' reads are audited. Each appends a `disclosure` receipt under the vault's governance events, and each fresh server creates its own receipt instance directory there. Vault walks skip that tree, so receipts do not reach ranking, but they change vault bytes.
   - The harness records the receipt instances that a vault holds at checkout. Before it compares a returned vault, it files each instance directory created since checkout into run evidence at the mirrored path, verifies the chains with the product's receipt reader, and records the instance, record and event-type counts. The quiescence record and the session facts carry the counts.
   - It then requires identical vault bytes and signatures, as amendment 4 says. An append to an instance that existed at checkout stays in the vault and refuses. Custody that lacks the recorded instances is compared without filing.
   - Audit stays on for every arm and the readiness probe stays. The harness refuses to run while `EXOMEM_KB_DIRNAME` is set, because the servers run without it and the receipt paths would differ.
   - Every session starts from identical bytes and signatures. This changes preparation only, never cases, keys, removals, arm budgets or the decision rule.

### Ready before held-out generation

1. `maps` mode built, and pinned to one release or commit for every arm.
2. The operator's consent to copy the vault into a snapshot and to sample ordinary turns from local session records.
3. The snapshot, removals, sweep and state builds done, with digests recorded.
4. The held-out key written, the groups fixed, and the held-out ids hashed.
5. The harness implementing A, M and P, with a dry run passed.
6. A judge route with quota, and an Opus route for the arm-guess audit.
7. A freeze commit that records the protocol, each arm's hashes, the hashed held-out ids, the groups and the power step's figures, made before the first held-out session.
8. The power step's figures for rules 1, 2 and 3, and the held-out read-intersection recorded as zero.

## Risks

- **A description is stale or poor.** Each line carries its generation, a derived description is marked, and stale projections are served marked.
- **The map grows with the vault.** The map is a bounded projection, and domain maps and recall hold the rest.
- **Invalidation fans out.** A highly connected anchor can invalidate many projections. Refresh coalesces and rebuilds lazily on read when cheaper.
- **Hidden material leaks through aggregates.** D7 computes over the caller's view before synthesis, and the counterfactual twins check it.
- **The evaluation flatters an arm.** The protocol fixes agent, budget, snapshot and scoring before generation, and ordinary turns sampled without regard to any arm's outcome guard against the selection of miss cases.

## Open questions

- Whether `topics` and project-key declarations are dense enough for domain maps to reach notes. The first catalogue build's counts of members and unresolved declarations per topic answer it.
- Whether a turn slice adds value over the map alone.
- Whether the context catalogue earns its build and upkeep cost over pull instructions alone.
- Whether `ask_memory` queries before the first answer are enough, or a combined call saves latency.
