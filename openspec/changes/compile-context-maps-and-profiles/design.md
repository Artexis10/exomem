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

1. **Context catalogue.** One searchable set of entries, distinct from the existing anchor catalogue. Its entry kinds are topics, anchors, notes (pages that are none of the other kinds), threads (the newest episode revision per `ep-` key) and collections (Records and Planning). It is a derived sidecar of its own: disposable, machine-local, rebuildable from the vault alone, and never read by recall. Notes exclude the raw-material trees (Sources and Evidence), the schema and governance trees, and the archive trees that the product layout declares. Each entry has a one-line description, its memberships, its source refs and the generation it was built from. D11 fixes where descriptions come from.
2. **Map.** A bounded projection of the context catalogue, labelled as an index of what exists, not as material relevant to the turn: live topics, threads, live or pending anchors and collections, one line each with what it is, its state from the current-state authority with its as-of date and that date's source when known, and a ref. Activation in `maps` mode returns it. It does not depend on the turn.
3. **Domain map.** The projection of one topic's entries, read by ref. It is how cold material stays reachable when it is not on the map.
4. **Profiles, read by ref.**
   - An **anchor profile** is what the role lanes compile for one anchor today when an agent passes `anchor`: units by role with currency and refs, current state from Records first, typed relations, related pages and evidence pointers.
   - A **topic profile** compiles the same lanes over a topic's members: the topic's standing stances, preferences, constraints and conventions, the anchors active now, open loops and recent changes.
   Profiles are compiled on demand and cached by generation.
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

- **Store.** The catalogue is a machine-local sidecar under the vault's state directory. It copies the activation index's pattern: a generation token bumped inside each write transaction, a scoped wipe on a schema-version change, incremental updates by per-source signature, and the activation kill switch. Derived sync gains a catalogue component, with its dependencies declared next to the existing components.
- **Entry.** Ref, kind, title, description, description origin (`authored`, `registry`, `model`, `lede` or `none`), topics, source refs, lifecycle class, activity date and generation.
- **Description.** The first that exists: `authored`, the page's `summary` field (a collection's on its `_collection.md` page); `registry`, a topic's domain registry `description` field; `model`, a model-made description for the page's current content hash; `lede`, the page's structural lede cut to one line; `none`. `summary` is the field episode recaps already write and activation already reads, so the change adds no second description field. `model` and `lede` stay separate origins, so a development round can compare them.
- **Derived-description store.** Built only if D6's model backfill runs. A reserved vault tree with its own internal-state descriptor and owner module, as compiled history has. The shared find and recall walk, `read_memory`, directory listing, page reads and write path checks already refuse reserved paths, so none of them serves the store. The owner admits a stored description only to callers who can see the described page. Each stored description records the content hash it describes, its model and its date.
- **Lifecycle class and activity.** The class is the status registry's `class` attribute of the entry's status, read through the existing lifecycle-status helpers. Activity is the newest date visible to the caller among the page's `updated` field, its units' authored dates and, for a thread, its newest revision.
- **Map selection.** The map lists topics that have a visible member, ordered by their newest member activity. It then lists threads, collections and anchors whose class is `live` or `pending`, newest first, until the budget is spent. Its last line names the domain maps for the rest. Entries whose class is `superseded`, `retired` or `abandoned` appear only in domain maps. Ties break by ref, so the order is total. `exomem://map` pages through the whole ordered projection; activation returns its first part within the budget. The share of the budget per kind is tuned on development cases and lives in one named constant with its reason.
- **Line.** One line per entry: kind, title, description, the state with its as-of date and source when known, and the ref.
- **Refs.** `exomem://map`, `exomem://map/topic/<domain key>`, `exomem://map/unfiled`, `exomem://profile/topic/<domain key>` and `exomem://profile/anchor/<percent-encoded anchor ref>`.
- **Read.** `read_memory` on one of these refs returns the ref, the generation, a stale flag and at most 40 items, with an opaque continuation token bound to the view, the caller's view and the generation. A malformed, unknown, expired or differently bound token returns the existing `INVALID_CONTINUATION` refusal. A token bound to the same view and caller but an older generation returns `CONTINUATION_STALE`. A ref with no visible view returns `NOT_FOUND`.

## Evaluation

This protocol is frozen before any held-out generation. A later change is a numbered amendment that says what changed and why. An amendment made after held-out generation starts cannot change the decision rule; if it does, the run is diagnostic only.

### Decision and arms

- **Decision.** Change the default of `EXOMEM_ACTIVATION_PACKET` from `anchors` to `maps`, or keep `anchors`.
- **Arms.** Three arms. For each arm, the full text the actor sees is frozen with a hash: the server instructions, the tool descriptions, the client instructions and the hook output format. P gets a pull variant of the server instructions that does not mention `activate_context`.

  | Arm | Server mode | Pushed at each user turn | Tools | Instruction lines |
  |---|---|---|---|---|
  | A | `anchors` | what the deployed retrieve hook pushes | Exomem read tools | the current client instructions |
  | M | `maps` | what the `maps` retrieve hook pushes | Exomem read tools | read the map and pull before the answer |
  | P | `anchors` | nothing | Exomem read tools without `activate_context` | pull before the answer |

  The Exomem read tools are `activate_context`, `ask_memory`, `read_memory` and `browse_memory`. A is the deployed configuration, so M − A changes both the packet and the instructions. P isolates the map: it shares M's instructions and has no push. P lacks `activate_context`, and the report names that tool difference as a confound. The turn slice is not an arm here; it needs its own frozen evaluation.
- **Actor.** One pinned Claude Sonnet model ID and one pinned client version, headless, on the subscription with no API key in the environment. It has no web, file or shell tools. Its instructions tell it not to name its tools in the answer.
- **Session.** The actor plays a case's quoted user turns in order in one fresh session. The scored answer is the answer to the turn that the key marks; for a single-turn case it is the first answer. No arm receives refs, `focus` or state from outside the session. The push at each user turn does not count against the pull budget.
- **Pull budget.** At most 8 Exomem calls per user turn. A call over the budget is refused, and the actor answers.
- **Order.** The arm order rotates per case, and sessions of different arms interleave.

### Cases and key

- **Miss cases.** Field reports of activation misses, turned into cases under the corpus split rule. The development split is for iteration. The held-out split is fixed at freeze as a hashed list of case ids and stays unread while iterating.
- **Ordinary turns.** First user turns of real sessions, sampled at random from the operator's local session records over a fixed window before freeze, without regard to any arm's outcome. They are keyed like miss cases, and some expect no memory. They guard against harm, because miss cases were selected for A's failure. The sample splits into development and held-out halves. The held-out half holds at least 24, and the power step below may raise that number.
- **Synthetic cases.** The deterministic activation audit's positive cases and negative twins, on its synthetic vault, through the agent-in-the-loop arm harness. Gold maps to required facts, and poison maps to forbidden claims. The harness implements A, M and P, and a dry run passes before freeze.
- **No-memory cases.** Held-out ordinary turns and held-out miss cases whose key expects no memory, and the negative twins.
- **Groups.** The key writer fixes the groups before generation: cases from one incident thread form one group, and each negative twin joins its positive.
- **Key.** An independent key writer keys every field case against the snapshot only, with refs into it, at the snapshot's time. For each case it records the required current facts and constraints with their source refs, the forbidden claims, the accepted successors of each required fact, and the excluded refs (pages that record the miss itself). The measured session never writes or reads the held-out key.

### Vault and state

1. Take one digest-pinned snapshot copy of the vault. The key writer writes the key on this copy.
2. Remove the incident records, the evaluation pages and every excluded ref from the copy. For each ordinary turn, the excluded refs include, selected by provenance and not by wording, every page and episode revision that the sampled session wrote: its session id, its episode key and its write receipts. They also include any later page that records that session's outcome.
3. Search the copy for every case id and every quoted turn string, and record zero hits per case. A page with a hit is removed or its case is struck before freeze.
4. Check every source ref in the key against the copy. A case whose required source was removed is struck before freeze.
5. Run the description coverage pass (D6) on the copy.
6. Build each arm's state once from the copy and record its digest. Every session starts from a fresh copy of that state.

### Measures and judging

- **Primary measure.** The scored answer uses every required fact and constraint, and asserts no forbidden claim as current. A newer fact passes for a required item only when the key lists it as an accepted successor. A different page that states the same required fact counts. A case's score is the mean over its repeats.
- **Secondary measures.** Expected material reached before the answer; off-target share of pushed and pulled refs; Exomem calls, total input tokens including pushed bytes, and wall time per case; failures by kind. The report names the tilt in call counts: M's map arrives by push, which costs no call. Complete-turn and partially quoted cases are reported separately.
- **Contamination.** On no-memory cases, the answer asserts user-specific facts that the turns do not supply. An arm contaminates a case when the judge finds contamination in a majority of the case's repeats.
- **Judge.** The newest Codex `-sol` model at its highest effort, on the Codex subscription, because the actor is a Claude model. The judge is chosen before judging starts, and one judge judges every item. If that route has no quota, a fresh Opus lane judges, and the report names the same-family limitation. For every arm the judge sees the same view: the user turns, the key's required items, forbidden claims and successors, and the answer under an opaque shuffled id, with `exomem://` refs replaced by opaque tokens. It never sees the arm key. It answers through a structured-output schema:
  - each required item: used, missing or contradicted;
  - each forbidden claim: asserted as current, mentioned as past, or absent;
  - on a no-memory case: whether the answer asserts user-specific facts that the turns do not supply, with the span.
- **Judge noise.** The judge re-judges a random fifth of the items. The report gives agreement per verdict kind. Agreement below 0.8 on required-item verdicts triggers the recovery path: a second judge from the other model family re-judges every item blind, with the re-judged items mixed in. An item's verdict stands where both judges agree. Where they disagree, the verdict that weighs against the switch stands.

### Runs and failures

- **Repeats.** Three per case per arm.
- **Timeouts.** 300 seconds per user turn.
- **Harness faults.** A rate limit, a transport error or a host fault is a harness fault. A crash of the Exomem server under test is not: it is an actor failure. The session is retried with the same inputs up to twice, and every attempt is logged. A case that still faults is struck for every arm. More than two struck cases void the run.
- **Actor failures.** An actor that gives no answer within the turn limit, exceeds the timeout, or loses its Exomem server to a crash scores 0, and the case stays in the denominator.
- **Usage.** Before the run, measure two cases end to end and report the projected usage. The actor and the judge run on subscriptions, so the run has no metered spend. When a quota runs out, the run pauses and resumes with unchanged inputs.

### Statistic and decision rule

- **Test.** For each pair of arms, the per-case score difference, averaged over cases. Within a group, case differences sum, so the statistic stays the mean over cases. Superiority is a one-sided sign-flip permutation test over groups at 0.025. Non-inferiority within a tolerance δ uses the same test on the difference plus δ. The report also gives a 95% interval from inverting the test.
- **Power.**
  - Rule 1: with a dozen held-out miss cases scored 0 or 1, each its own group, M must win at least 6 cases with no loss to show superiority over A, or 8 with one loss. Fewer groups than cases need more wins. The smallest effect the rule can detect is therefore about +0.5. Development evidence puts M − A above that at retrieval level (+0.67) and M − P close to zero.
  - Rule 2: before freeze, measure the per-case spread between repeats on the development ordinary turns. Simulate equal arms with that spread, and set the held-out size (at least 24) and δ (at least 0.10) so that equal arms pass rule 2 at least 80% of the time. The freeze commit records the spread, the size, δ and the simulated pass rate.
- **Rule.** Switch only when all of these hold:
  1. On held-out miss cases, M beats A.
  2. On held-out ordinary turns, M is non-inferior to A within δ, as the power step sets it.
  3. M pays over P on held-out miss cases and ordinary turns together: M beats P, or M is non-inferior to P within δ = 0.10 and M beats P on Exomem calls per case under the same test.
  4. On no-memory cases, M contaminates no more cases than A does, plus at most one.
  5. M loses no synthetic positive case that A passes in a majority of its repeats. M loses a case when it fails that case in a majority of its own repeats.
- **Expected outcome.** Rule 3 most likely passes, if at all, through fewer calls. If P beats A and M does not pay over P, the default stays `anchors`, and the report proposes a follow-up change for pull-first instructions without the map.
- **Chain checks.** Before judging, check the snapshot digest and the removals, each arm's captured inputs against its frozen hashes, every case present in the outputs with failures named, and the judge inputs against the captured inputs. After judging, check the unblinding key against the item ids and recompute the scores from the raw verdicts.

### Ready before held-out generation

1. `maps` mode built, and pinned to one release or commit for every arm.
2. The operator's consent to copy the vault into a snapshot and to sample ordinary turns from local session records.
3. The snapshot, removals, sweep and state builds done, with digests recorded.
4. The held-out key written, the groups fixed, and the held-out ids hashed.
5. The harness implementing A, M and P, with a dry run passed.
6. A judge route with quota.
7. A freeze commit that records the protocol, each arm's hashes, the hashed held-out ids, the groups and the power step's figures, made before the first held-out session.

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
