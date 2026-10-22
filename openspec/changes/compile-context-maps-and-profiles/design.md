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

1. **Context catalogue.** One searchable set of entries, distinct from the existing anchor catalogue. Its entry kinds are topics, anchors, notes (pages that are none of the other kinds), threads (the newest episode revision per `ep-` key) and collections (Records and Planning). It is a derived sidecar of its own: disposable, machine-local, rebuildable from the vault alone, and never read by recall. Notes exclude Sources, Evidence, `_Schema` and archived pages. Each entry has a one-line description, its memberships, its source refs and the generation it was built from. D11 fixes where descriptions come from.
2. **Map.** A bounded projection of the context catalogue, labelled as an index of what exists, not as material relevant to the turn: live topics, threads, key anchors and collections, one line each with what it is, its state from the current-state authority with its as-of date and that date's source when known, and a ref. Activation in `maps` mode returns it. It does not depend on the turn.
3. **Domain map.** The projection of one topic's entries, read by ref. It is how cold material stays reachable when it is not on the map.
4. **Profiles, read by ref.**
   - An **anchor profile** is what the role lanes compile for one anchor today when an agent passes `anchor`: units by role with currency and refs, current state from Records first, typed relations, related pages and evidence pointers.
   - A **topic profile** compiles the same lanes over a topic's members: the topic's standing stances, preferences, constraints and conventions, the anchors active now, open loops and recent changes.
   Profiles are compiled on demand and cached by generation.
5. **First-answer workflow.** The agent reads the map, opens the domain maps and profiles that the turn needs, searches with its own words when the map does not cover the need, then answers and cites refs.
6. **Turn slice (an evaluation arm, not yet a contract).** Profiles selected by meaning from the turn and attached to the map. It ships only if it beats the map alone.

## Decisions

### D1. Map-assisted pulls are the first-answer workflow

The evidence supports an agent that sees what exists and pulls. Activation in `maps` mode returns the map; client instructions make reading it and pulling before the first answer the supported workflow. The map is the only pushed layer until an arm shows that more push helps.

### D2. One catalogue, many projections

The map and the domain maps are projections of one context catalogue, not separately maintained documents. Profiles are compiled from the existing role lanes, not stored as a second copy of the units. This keeps one source for each fact and one place to invalidate.

### D3. Topic membership is a declared contract

A topic is a domain from the vocabulary registry. Membership comes from three declarations, and each reuses a field that exists today:

- **Page field.** The page's `domain` field, a value or a list. Source capture writes it today, and the lexstore already indexes it as a value and as a list.
- **Project key.** The project-key registry (`project-keys.yaml`) gains an optional `domain` field per key, a value or a list. A page's project keys are its `project` and `projects` fields, read the way project anchors read them today. The loader resolves the new field and includes it in the registry's content hash, so a change rebuilds what depends on it.
- **Relation.** An edge in the epistemic graph whose relation kind has the registry family `membership` (today `member_of`). The entry joins the topics that the edge's target declares through its own page field or project key.

Every declared value resolves through the domains registry by key, label or alias. A value that does not resolve gives no membership, and the catalogue counts it as an unresolved declaration, so a typo is visible instead of guessed. Writes that set `domain` on any page resolve it through the same resolver that source capture uses. Membership runs from the entry to the topic and is not transitive: an entry never joins a topic that its relation's target reaches only through its own relations. A mention of an anchor does not import the anchor's topics. An episode thread belongs to the topics its revision declares, and is otherwise unfiled; its `about` refs are not membership. Ordinary notes become context-catalogue entries in the topics they declare, so a domain map reaches notes that are not anchors. Membership rules live in the registries; code branches only on the closed attributes they declare.

### D4. Current state has one authority

Map lines and profiles take state from the existing current-state resolver: Records first, then declared state pages (as the pending `add-activation-temporal-currency` change defines them, which lands first), then observations. Units keep their own authored dates; a page's modification date does not become a unit's as-of date. A state line shows the resolver's as-of date together with the source it came from, because the resolver can take a profile page's `updated` field as the as-of date of that page's declared state. An entry with no known state shows none.

### D5. Descriptions are written at write time and invalidated by generation

When a write creates or changes a context-catalogue entry, the writing agent may supply its one-line description, which the write stores in the page's `summary` field. Otherwise the catalogue derives one and marks it as derived (D11). Refresh runs through derived sync with explicit reverse dependencies: old and new memberships, incoming relations, deletion, rename, supersession, registry changes and external edits. Refresh coalesces per entry and never publishes a projection built from an obsolete generation. A projection whose inputs changed and are not yet rebuilt is served marked stale.

### D6. Description coverage is a launch dependency

Every entry has a description before any evaluation of the map: authored, from the registry, or derived from the page's structural lede. The lede costs nothing and covers every page with a body, so it is the first backfill. A model-made backfill into the derived-description store runs only if a development round shows that lede descriptions lose against authored ones. That backfill is resumable, records its provenance and cost, and reports coverage. The catalogue reports coverage by description origin, and an entry with no description stays visibly undescribed.

### D7. The caller's view applies before compilation

The catalogue, map, domain maps and profiles are computed over the caller's view: hidden inputs never reach synthesis, selection, ranking, ordering, caps, counts, activity scores, staleness marks or generation tokens. A restricted caller's projections are compiled for that view and bypass any shared cache. Counterfactual twins prove it: adding or changing a withheld entry leaves a caller's map, domain maps, profiles and their ordering unchanged. The egress guard learns the new blocks and fields.

### D8. Refs to derived views resolve through one read contract

`read_memory` resolves `map`, domain-map and profile refs with bounded output, pagination for overflow, and explicit missing and stale states. Activation and every surface return the same shapes.

### D9. One packet budget

The map and any slice share the request's `max_chars` cap. Pulls have their own budget per answer. Overflow becomes bounded, paginated refs, never cut claims.

### D10. The `anchors` mode stays until a frozen evaluation decides

The process setting `EXOMEM_ACTIVATION_PACKET` selects `anchors` (default) or `maps`. Requirements that cover read-only operation, governance, freshness, latency and surface parity bind both modes; anchor resolution and the anchors packet shape bind `anchors` mode only. The default changes only through the frozen evaluation below and the normal release line; the setting is the undo.

### D11. The catalogue contract

- **Store.** The catalogue is a machine-local sidecar under the vault's state directory. It copies the activation index's pattern: a generation token bumped inside each write transaction, a scoped wipe on a schema-version change, incremental updates by per-source signature, and the activation kill switch. Derived sync gains a catalogue component, with its dependencies declared next to the existing components.
- **Entry.** Ref, kind, title, description, description origin (`authored`, `registry`, `derived` or `none`), topics, source refs, lifecycle class, activity date and generation.
- **Description.** The first that exists: the page's `summary` field (a collection's on its `_collection.md` page, a topic's in its domain registry entry's `description` field); a model-made description for the page's current content hash; the page's structural lede, cut to one line; none. `summary` is the field episode recaps already write and activation already reads, so the change adds no second description field.
- **Derived-description store.** A reserved vault tree with its own internal-state descriptor and owner module, as compiled history has. The shared find and recall walk, `read_memory`, directory listing, page reads and write path checks already refuse reserved paths, so none of them serves the store. The owner admits a stored description only to callers who can see the described page. Each stored description records the content hash it describes, its model and its date.
- **Lifecycle class and activity.** The class is the status registry's `class` attribute of the entry's status. Activity is the newest authored date visible to the caller among the page's `updated` field, its units' authored dates and, for a thread, its newest revision.
- **Map selection.** The map lists topics that have a visible member, ordered by their newest member activity. It then lists threads, collections and anchors whose class is `live` or `pending`, newest first, until the budget is spent. Its last line names the domain maps for the rest. Entries whose class is `superseded`, `retired` or `abandoned` appear only in domain maps. The share of the budget per kind is tuned on development cases.
- **Line.** One line per entry: kind, title, description, the state with its as-of date and source when known, and the ref.
- **Refs.** `exomem://map`, `exomem://map/topic/<domain key>`, `exomem://map/unfiled`, `exomem://profile/topic/<domain key>` and `exomem://profile/anchor/<percent-encoded anchor ref>`.
- **Read.** `read_memory` on one of these refs returns the ref, the generation, a stale flag and at most 40 items, with an opaque continuation token bound to the view, the caller's view and the generation. A token from another generation returns `CONTINUATION_STALE`. A ref with no visible view returns `NOT_FOUND`.

## Evaluation

This protocol is frozen before any held-out generation. A later change is a numbered amendment that says what changed and why.

- **Decision.** Change the default of `EXOMEM_ACTIVATION_PACKET` from `anchors` to `maps`, or keep `anchors`.
- **Sampling unit.** One case, played as one session. Cases from one incident thread form a group, and the interval resamples groups.
- **Cases.**
  - Field cases: field reports that an independent key writer turns into cases under the corpus split rule. The development split is for iteration. The held-out split is fixed when held-out generation starts, and it holds every report filed by then under the same rule. It stays unread while iterating.
  - Synthetic cases: the deterministic activation audit's positive cases and negative twins, played through the same arms on the audit's synthetic vault with the existing agent-in-the-loop arm harness.
  - No-memory cases: the negative twins and every field case whose key expects no memory.
- **Key.** For each field case, the independent key writer records from source pages only: the required current facts and constraints with their source refs, the forbidden stale or off-target claims, the accepted successors of each required fact, and the excluded refs (pages that record the failure itself). The measured session never writes or reads the held-out key.
- **Vault.** One digest-pinned snapshot copy, taken once before generation. The copy omits the incident records, the evaluation pages and every excluded ref. Every arm runs on the same copy, each with an empty state root of its own.
- **Arms.** Every arm uses the same actor, tools, budget and instructions, except for the arm's own lines:

  | Arm | Pushed at session start | Instruction lines |
  |---|---|---|
  | A | the `anchors` packet, through the retrieve hook | the current client instructions |
  | M | the map | read the map and pull before the first answer |
  | S | the map and the turn slice, only if the slice is built | as M |
  | P | nothing | pull before the first answer, with no map |

- **Actor.** Claude Sonnet at its default effort, headless, on the subscription, with only the Exomem read tools (`activate_context`, `ask_memory`, `read_memory`, `browse_memory`) and no web, file or shell tools.
- **Session.** The actor plays the case's quoted user turns in order in one fresh session. The scored answer is the answer to the turn that the key marks. No arm receives refs, `focus` or state from outside the session.
- **Pull budget.** At most 8 Exomem calls per user turn. A call over the budget is refused, and the actor answers.
- **Repeats.** Three per case per arm. A case's score is the mean over its repeats.
- **Primary measure.** The scored answer uses every required fact and constraint, and asserts no forbidden claim as current. A newer fact passes for a required item only when the key lists it as an accepted successor. A different page that states the same required fact counts. A session that fails, times out or ends without an answer scores 0 and stays in the denominator.
- **Secondary measures.**
  - Expected material reached: an expected ref or accepted successor read before the scored answer.
  - Off-target share: pushed or pulled refs that are neither expected nor accepted successors.
  - Contamination on no-memory cases: the answer presents vault material as relevant to the turn.
  - Exomem calls, tokens and wall time per case.
  - Failures, by kind.
- **Judge.** The newest Codex `-sol` model at its highest effort, on the Codex subscription, because the actor is a Claude model. If that route has no quota, a fresh Opus lane judges and the report names the same-family limitation. For every arm the judge sees the same view: the user turns, the key's required items, forbidden claims and successors, and the answer under an opaque shuffled id. It never sees the arm key. It answers through a structured-output schema with one verdict per required item: used, missing or contradicted. It re-judges a random tenth of the items to measure its own noise.
- **Statistic.** For each pair of arms, the mean over cases of the per-case score difference, with a 95% percentile bootstrap interval over case groups (10,000 resamples, seed 20261022).
- **Decision rule.** Switch only when all of these hold:
  1. On held-out field cases, the lower bound of M − A on the primary measure is above 0.
  2. On held-out field cases, the lower bound of M − P on the primary measure is above 0.
  3. On no-memory cases, the upper bound of M − A on the contamination rate is at most 0.10.
  4. On synthetic positive cases, the lower bound of M − A on the primary measure is at least −0.10.

  If S runs, the same rule first decides between S and M, and the winner then faces A and P.
- **Sample size.** A dozen held-out field cases can show only a large effect. The report says so and gives each arm's absolute rates.
- **Spending.** The actor and the judge run on subscriptions, so the run has no metered spend. Before the run, measure two cases end to end and report the projected usage.
- **Ready before held-out generation.** `maps` mode built and pinned to one release or commit for every arm; the held-out key extended with required facts, successors and excluded refs; the operator's consent to copy the vault into a snapshot; a judge route with quota.

## Risks

- **A description is stale or poor.** Each line carries its generation, a derived description is marked, and stale projections are served marked.
- **The map grows with the vault.** The map is a bounded projection, and domain maps and recall hold the rest.
- **Invalidation fans out.** A highly connected anchor can invalidate many projections. Refresh coalesces and rebuilds lazily on read when cheaper.
- **Hidden material leaks through aggregates.** D7 computes over the caller's view before synthesis, and the counterfactual twins check it.
- **The evaluation flatters an arm.** The protocol fixes agent, budget, snapshot and scoring before generation.

## Open questions

- Whether `domain` declarations on pages other than sources are dense enough for domain maps to reach notes. The first catalogue build's counts of members and unresolved declarations per topic answer it.
- Whether a turn slice adds value over the map alone.
- Whether the context catalogue earns its build and upkeep cost over pull instructions alone.
- Whether `ask_memory` queries before the first answer are enough, or a combined call saves latency.
