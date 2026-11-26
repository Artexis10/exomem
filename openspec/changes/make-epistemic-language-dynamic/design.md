## Context

The rule in `AGENTS.md` is that every category set lives in a vocabulary registry. Code branches only on closed attribute values that a registry schema declares and code implements (`family`, `role`, status `class`). It never branches on an entry key.

Relation types follow the rule in part. They are registry entries, and a namespaced extension must name one core parent and inherit its family (`family_mismatch`). But code branches on particular family values and relation keys. Kinds and verdicts do not follow the rule at all.

This change applies the rule to the whole epistemic language. For relation words it narrows the rule: code reads behaviours, never a family value (see "The rule for every inventory entry").

## Decisions

### Three layers: words, behaviours, dimensions

- **A word** is a registry entry: a relation type, a kind, a verdict, a modality, a belief level, an inference mode. It has a key, labels per language, a description and examples. It keeps its own meaning. `refines` and `contradicts` both bear on a claim, but they say different things and stay distinct entries.
- **A behaviour** is something code does. The registry schema declares each closed set, and code implements each member. Adding one is a code change and a schema change, and that is the point: a behaviour is a promise code keeps. There are three sets, because each has its own target: relation behaviours, kind traits and dimension display behaviours.
- **An epistemic dimension** groups words by the question they answer. Dimensions are registry data, and a vault can add one.

**Why this split:** the owner asked whether the language can be fully dynamic. It can be dynamic in everything except what code does with a meaning. Making behaviours data too would mean code that runs arbitrary rules from a registry, and that turns a vocabulary into a programming language. Small closed sets keep the code honest, and every word above them stays free.

### Relation behaviours live on core relation entries

A core relation entry gains a `behaviours` list, beside its existing `presentation_role`. An extension inherits its parent's behaviours and cannot set its own, just as it inherits the parent's family. Vaults do not create families.

Behaviours sit on relations, not families, because today's sets are keyed both ways. The two members of an inverse pair can need different behaviours: the graph stores each edge as written, so `precedes` and `follows` carry opposite order behaviours, and code never has to ask which key it holds. Each behaviour replaces one set.

Table rows cite the 2026-11-08 language inventory by ID: F for a branch on a family value, K for a branch on a relation key, C for a branch on a kind. Line numbers refer to `afe4bffdd`.

| Behaviour | What code does | Replaces (inventory) | Core relations |
|---|---|---|---|
| `lifts` | A unit relation may become a page-level proposal. | F1 `_LIFT_RELATION_FAMILIES` | answers, resolves, raises_question, supports, contradicts, refines, evidenced_by, duplicates |
| `membership` | The catalogue reads the edge as the source belonging to the target. | F2 `MEMBERSHIP_FAMILY` | member_of |
| `bears_on` | A relation authored on a prediction or question unit counts toward settling it. Inbound edges and edges from elsewhere on the page do not. | K2 `_PREDICTION_RESOLVING_RELATIONS`, `_QUESTION_ANSWERING_RELATIONS` | supports, contradicts, resolves, evidenced_by |
| `settles` | The edge settles its target. The graph pairs pages that settle one target, and stance treats such pages as a declared pair. | K1 `_RESOLUTION_RELATION_TYPES`; K17 `ANSWERS` | answers, resolves |
| `sources` | The edge is provenance. Activation counts it, and entity hydration never treats it as a content link. | K3 `_PROVENANCE_RELATIONS`; K12 `derived_from` exclusions | derived_from, evidenced_by, cites |
| `replaces` | The target is no longer the current view. Currency, role review, the sensed model and census chains follow the edge. It is the only behaviour through which a frontmatter edge qualifies in relation review. | F5, F6, K5, K6, K9 (chain), K11 | supersedes |
| `corrects` | An edge into a unit surfaces its source as a counterpart beside the unit. An episode write must pin the edge's exact current target. | F3 `CORRECTIVE_FAMILIES` | contradicts, refines, supports, supersedes, resolves, answers |
| `associative` | Relation review never counts the edge as a typed edge, because it records a link, provenance or a mention, not a claim about the target. | F4 `_EXCLUDED_FAMILIES` (drops the dead `provenance`) | links_to, cites, derived_from, evidenced_by, mentions, observed_in |
| `plain_link` | The edge is a plain link. It ranks after typed edges, never counts as typed, and the census skips it. | F10, F11, K16 readers (projection_runtime 1825, vocabulary_projection 164, relation_census 64/902/1542) | links_to |
| `opposes` | Source and target are rivals. Stance reads the pair as declared, the census flags it inside one supersession chain, and the audit raises it as an asserted contradiction. | K17 `CONTRADICTS`, K9 | contradicts |
| `names_subject` | The edge names the entity a unit is about. Restricted working-set admission matches it against the turn's subject. | K20 | about_entity |
| `names_assessed_claim` | The edge names the claim a belief unit assesses. A unit with the `credence` trait needs one. | new | assesses |
| `earlier_than_target` | The source comes before the target in time. | new | precedes |
| `later_than_target` | The source comes after the target in time. | new | follows |

An extension's behaviours resolve through its core parent, never through a saved family. `assesses` carries only `names_assessed_claim`, and `precedes` and `follows` only the order pair. Relation review counts all three as typed edges, because none is `associative`.

`presentation_role` stays renderer metadata: it tells a viewer how to draw an edge. Behaviours decide what code does with the edge. `supersedes` carries both, for those two different reasons. `presentation_role` cannot carry a promise, because an extension may set and later change it.

An inventory of every branch on a relation key or family value in `src/exomem` maps each branch to a behaviour, a binding or named debt, under the rule below. Extensions change on purpose: the intended-changes list names each change, and the fixture shows each one.

### The rule for every inventory entry

1. A reader whose set decides what a user or agent gets becomes a behaviour or trait read.
2. A census-only rule becomes a behaviour read only when an adopted behaviour already covers its set. Otherwise it stays as named debt.
3. A producer that writes one core key reads a registry binding when one exists, or when its copies must fold. Otherwise it stays a named producer constant.
4. Labels, teaching copy and frozen instrument schemas stay as named debt.
5. Code never names a family value. It branches on behaviours, traits, `role` and status `class`. Family stays meaning data that dimensions and traversal profiles read.

Rule 5 narrows the `AGENTS.md` wording "`family`, `role`, status `class`" for relation words. Family is the grouping that dimensions and profiles read, so a family read couples code to display grouping. Inverse pairs and cross-family sets (`settles`, `corrects`) need per-relation statements anyway. Two mechanisms for one concept caused the drift that the inventory found: F6 reads supersession by family, and K5 reads it by key.

Interfaces:
- `RelationRegistry.behaviours_of(key)` returns the entry's own behaviours for a core key and the parent's for an extension.
- `RelationRegistry.keys_with(behaviour)` returns core and extension keys.
- `RelationRegistry.frontmatter_fields()` and `frontmatter_relation(field)` expose the field binding (see "Frontmatter fields are registry data").
- The kind registry gets `traits_of(kind)` and `kinds_with(trait)`.
- SQL readers ask `exomem_edge_behaves(EDGE_COLUMNS, behaviour)`, which reads the edge's behaviours through its authoring page's registry. A reader that filters on stored keys expands them through `traversal_profiles.relation_query_plan`, as `relation_edges()` does, or keeps the candidate status, because an extension is stored as a candidate.

### Inventory decisions

1. **`replaces` has three shapes.** One `replaces` behaviour on `supersedes`, inherited through the parent, replaces the family shape (F5, F6) and the key shape (K5, K6, K11 and the chain half of K9). The frontmatter pointer shape is not a relation read: `supersedes` and `superseded_by` are lifecycle fields that `frontmatter_fields` binds to `supersedes`, so a graph edge from either field carries `replaces`. K21's pointer readers keep the field tokens, and the rule they repeat is debt.
2. **`lifts` is a per-relation behaviour** on the eight keys in the table, inherited through the parent. Core relations, and every extension whose family equals its parent's, do not change. A grandfathered extension with a different saved family now follows its parent. The pack keeps the causality reason as a YAML comment beside `causes` and `caused_by`: lifting one unit's causal claim would assert a mechanism between pages.
3. **Key-set mismatches.** Stance reads `settles` as well as `opposes`, so two pages that both `resolves` one target become a declared pair, as two pages that both `answers` one target already are. The graph already pairs pages that settle one target. If dogfood shows near-duplicate pairs that both resolve one target, stance reverts to `answers` only. The dreamer's content-link exclusion (K12) widens to the `sources` behaviour on purpose. K10, K13 and K14 mean "the Sources a page declares": they select by the `sources` field and take the relation from `frontmatter_relation("sources")`. There is no `declared_source` behaviour. The shared-source candidate copies in `epistemic_graph` and `relation_queue` stay separate; folding them is debt, because it changes suggestions.
4. **`frontmatter_fields` is a core-only relation attribute** that maps a field name to `forward` or `reverse`. The loader derives the `frontmatter` origin from it. `origins` says which producers may emit a relation, and `frontmatter_fields` refines the frontmatter producer to field and direction. Wikilink, semantic-unit and semantic-block producers need no binding, because their origin names the relation. They stay producer constants (debt). A vault cannot bind its own field yet.
5. **Kind traits gain `question`**, shipped on `open_question`. It folds the two differing sets C6 and C7, and the graph's shared-open-question generator reads it. `evidential` stays with its one census check as debt, and `comparable` is not added, because it would tie the frozen `units-v1` instrument to vault data. `closable` is declared in slice 5 with `flag_open`. C9 stays on category, because a unit's category and kind diverge when it carries an explicit `category:` row.
6. **Proposed relation behaviours.** Adopt `corrects`, `associative` (renamed from `plain_association`, so it does not read as a twin of `plain_link`), `plain_link`, `opposes` and `names_subject`. Reject `declared_source` (the concept is the `sources` field), `target_expects_evidence` and `target_expects_question` (census-only, and `target_kinds` is a hard scope that would drop edges from the graph), and `marks_duplicate` (a producer default that writes one core key). Keep `cross_page_only` and the affiliation count (F8) as debt, because each has census readers only.
7. **The four shipped traversal profiles move** into `src/exomem/vocabulary/packs/core/traversal-profiles.yaml`, in the vault overlay's grammar, with today's priority (sorted families). `all` stays computed from the core families, and the new families `order` and `assessment` join only `all`. Profiles carry direction, priority and depth, which dimensions lack, so they are not derived from dimensions.

### Frontmatter fields are registry data

| Relation | `frontmatter_fields` | `origins` written in the pack (the loader adds `frontmatter`) |
|---|---|---|
| derived_from | `sources: forward` | semantic_relation, semantic_block, semantic_unit |
| evidenced_by | `evidence: forward`, `evidences: forward`, `evidence_paths: forward` | semantic_relation |
| supersedes | `supersedes: forward`, `superseded_by: reverse` | semantic_relation |
| links_to | `related: forward` | semantic_relation, wikilink |

Rules:
- Only the core pack sets the attribute. It joins `_CORE_FIELDS`, not `_DEFINITION_FIELDS`.
- A field is bound to one relation, and its direction is `forward` (the page is the edge's source) or `reverse` (the page is its target).
- The loader adds `frontmatter` to the bound relation's `origins`. A core entry that writes `frontmatter` in `origins` itself fails the pack build.
- Readers: graph indexing (`_page_level_edges`), relation facts (`_derive_relation_facts`) and activation (`measure_document`). Activation counts a field as typed when its relation lacks `plain_link`.

### Kind traits are a separate closed set

Kinds need their own set, because a relation behaviour has a source and a target and a kind has neither. A kind entry gains a `traits` list:

| Trait | What code does | Replaces (inventory) | Shipped kinds |
|---|---|---|---|
| `assertion` | Activation counts the unit as an assertion. `inference_mode` is valid only on such a unit (slice 4). | C2 `_ASSERTION_BLOCK_TYPES` | claim, finding, inference, hypothesis, result |
| `page_claim` | The claims reader takes the first such unit as the page's claim. | C3 | claim |
| `procedure` | Role review reads the unit as a procedure. The category fallback stays as debt. | C4 kind leg | procedure |
| `outcome` | Role review reads the unit as an outcome. The category fallback stays as debt. | C5 kind leg | result |
| `question` | The graph's shared-open-question generator and the census read the unit as a question. The category leg stays as debt. | C6 kind leg, C7 | open_question |
| `closable` | `flag_open` marks the unit while no verdict or answer exists. Declared in slice 5 with `flag_open`. | new | open_question, hypothesis, prediction, assumption |
| `credence` | The unit takes `credence`, `credence_holder` and `credence_basis`, and the belief-unit rules apply to it. Those rows on a unit of another kind are an error (slice 4). | new | belief |
| `dated_event` | The unit takes `occurred_at`. The row on a unit of another kind warns for one release (slice 4). | new | timeline_event |

A trait never schedules a check. `check_by` already does that on any unit, so a trait for it would duplicate the governed key.

The 27 built-in kinds in `semantic_blocks.py` move into the shipped pack. `definition` and `procedure` are among them already. The kind debt is in the debt table.

### Verdicts are state

Verdicts become a registry with shipped defaults (abandoned, confirmed, inconclusive, qualified, refuted), labels and descriptions. A vault can add a verdict or translate one. An experiment page's `outcome:` shares the verdict vocabulary today through one constant, so it follows the registry too: a vault-added verdict is a valid outcome. Outcomes take keys only, and unit verdicts also accept labels, which resolve to the same keys. Verdicts carry no behaviours. A verdict changes no lifecycle, standing or rank, as today, and a refuted claim keeps active standing. Role review held three checks on verdict keys that no shipped verdict has (`retracted`, `false`, `failed`). They were unreachable, because validation refused those values, so this change removes them rather than let a vault verdict gain behaviour by its key.

### Epistemic dimensions

A dimension entry has a key, labels, a description, its members and its display behaviours. Members are relation families, unit kinds and governed metadata keys. One member may sit in several dimensions, because a word can answer more than one question. A unit belongs to a dimension when its kind is a member, when it carries a member key, or when it is an endpoint of an edge whose family is a member.

**Every dimension gets the generic operations:** retrieval filters by it, the context compiler groups by it, and reviews and reports queue by it. No code names a particular dimension.

Two surfaces group by dimension now: unit search results and the context pack. A search asks for groups through its filter alone. When the filter names `unit.dimension` at the top level, alone or under a top-level `$and`, `$eq` returns one group and `$in` returns one group for each named dimension, in the order given. `$ne` asks for no group, and the limit counts distinct units. The automatic working set (`recall_context`) and the retrieve-nudge hook do not group or mark yet. They wait for the real-model check (task 5.4). The per-dimension report row counts each mark that the dimension's display behaviours produce.

**A dimension may also name display behaviours** from a closed set:

- `lead_with_current`: put current members before replaced ones, and mark the replaced ones. A member is replaced when its page's lifecycle class is history, or when an inbound edge whose relation carries `replaces` ends at it or at its page; that edge's source is its successor;
- `show_origin`: show beside each member where it came from and whose it is. `by` is its `asserted_by` or `credence_holder`, and `from` names up to three targets of its outbound edges, and of its page's, whose relation carries `sources`;
- `flag_open`: mark members whose kind has the `closable` trait and that have no verdict or answer yet. An answer is an inbound edge whose relation carries `settles`;
- `order_in_time`: present members in time order, read from edges with `earlier_than_target` or `later_than_target` and from `occurred_at`. It is the only rule that orders events.

A display behaviour applies only within its own dimension's group. When a request groups by several dimensions, a unit that belongs to more than one appears in full in the first requested dimension and as a ref in the others, so orders cannot conflict.

Display behaviours change only order and annotation in compiled context. They never hide a unit, change its standing or change what a word means. When the pack's budget is short, a replaced group member stays in its group as a ref. The automatic working set is a different reader: it drops a superseded unit when its successor is already in the packet, and this change does not alter that. A dimension change takes effect only after a reviewed registry save, as every registry change does.

Every mark comes from the edges that the request read for its group membership, in the caller's view, so a page withheld from the caller changes no mark: `open` means that nothing the caller can see settles the unit. When that edge read is partial or unavailable, no unit is marked `open`, no edge reorders a group, and the result says that the edge state is unknown.

The shipped dimensions:

| Key | Question | Members | Display |
|---|---|---|---|
| `currency` | Is it still current? | supersession | `lead_with_current` |
| `correction` | Was it corrected? | contradiction, testing, `verdict` | none |
| `provenance` | Where did it come from? | derivation, evidence, citation, observation | `show_origin` |
| `attribution` | Whose is it? | `asserted_by`, `credence_holder` | `show_origin` |
| `kind` | What kind of statement is it? | every kind, `modality`, `inference_mode` | none |
| `argument` | What is the case for and against? | support, contradiction | none |
| `dependency` | What does it rest on? | dependency, causality, blocking, implementation, mitigation, use, production | none |
| `entities` | What and whom is it about? | entity, membership, kinship, affiliation, ownership, operation, supply, location, composition | none |
| `identity` | Is it the same thing? | duplication, specialization | none |
| `scope` | Where does it hold? | refinement | none |
| `uncertainty` | How sure are we? | question, answer, resolution, assessment, kinds belief, hypothesis, assumption, prediction, open_question and risk, `check_by`, `credence` | `flag_open` |
| `order` | When did it happen? | order, kind timeline_event, `occurred_at` | `order_in_time` |

The generic families mention, link and relation belong to no dimension.

Task 5.5 first moves dimensions without family members before lane caps and the final limit.
The lexical catalogue stores authored governed-key presence in `unit_row_keys(parent_path, occurrence_key, row_key)`.
Its primary key covers all three columns; an index starts with `row_key`.
The projection uses neutral occurrences, because admitted kinds can carry rows that the neutral candidate interpretation rejects.
It stores no dimension, family or resolved meaning.

Existing stores acquire the projection through owner generation 5 of the current bounded rederivation job.
Each pass records its owner generation beside its target and cursor.
A missing or different generation restarts from the first page; an interrupted pass in the same generation resumes its cursor.
The final batch alone stamps the generation current.
The collection schema stays unchanged, so this additive projection creates no newer-schema boundary for older readers.

A compiled proposal that consumes `unit_row_keys` waits for that final stamp, including negation and Boolean branches.
It returns the existing typed retrieval warming outcome until the rows are whole.
This prevents incomplete presence facts from becoming falsely complete matches or exclusions.
A wrong refusal temporarily costs only the dependent caller while the normal background job completes.
Unrelated recall and simplified kind-only dimensions remain available.
Readiness uses each projection's introduction generation from the existing owner mechanism.
Inbound links require generation 1, so complete inbound rows stay available during the generation 5 row-key pass.
The same job records the completed parser version separately, using `semantic_index.PARSER_VERSION` as its required value.
Missing, malformed or different parser records require a full bounded pass without owner DDL when the owner generation is current.
Each pass records both generations; it resumes only when both match the running build.
Migration records both generations after fresh verification, and only the final full batch updates them thereafter.
Ordinary page writes derive their own rows but never qualify the whole corpus.
Generation repair preserves its pinned target across head writes; a later overlay change remains owed after completion.
Overlay-only passes still restart when their target changes.
The bounded path domain includes stored pages and already-published page or identity subjects, including unowned files.
The existing source getter reads stored canonical bytes or current unowned bytes; the importer keeps ownership of unseen-file discovery.

Stored semantic recall, FTS, unit filters, occurrence binding and selected graph consumers qualify parser inputs before candidate SQL or cached misses.
Each pinned store snapshot reads the generation records once; cache identity includes the required parser version.
An incomplete parser pass returns the existing typed warming or unavailable outcome, and semantic aggregates report unknown counts.
This prevents old parser summaries from becoming falsely authoritative empty answers.
A wrong refusal temporarily delays semantic readers while the existing background pass finishes.
Raw pages, existence, path lists and complete inbound links remain available; ordinary registry lag retains its notice without a refusal.
Status exposes the completed and required parser versions.
The stored candidate now applies indexed proposals and exact selected membership to kinds, keys and families before lane caps.
Family preparation uses neutral graph inputs and indexed target proofs under the same pinned snapshot.
The file-mode family fallback retains its existing after-limit caveat until its own indexed path is delivered.
The managed file runtime keeps its existing freshness and admission contract.

The filter keeps its original expression and a separate candidate projection.
Selected `UnitStructure` values decide carried keys before lexical ranking and the vector allow-set.
Authored presence remains unknown in candidate SQL until selected interpretation, so `$not` and `$ne` cannot complement a superset into an under-selection.
Registry edits need no corpus rebuild.
File-mode dimensions with family members keep their after-limit check and notice.
Task 5.5 remains open through independent review, production-sized latency and final delivery proof.

This reuses SQLite, the structured-filter planner and selected-unit adapters.
The gap is the missing row-key projection and the dimension check after the limit.
The added maintenance covers sparse authored keys, neutral graph inputs and resolver keys through existing builders, store derivation and verification.
The design follows `Effective context engineering` for request context and `Writing effective tools` for indexed operations.
The proof uses better-ranked nonmembers, invalid rows, admitted kinds, Boolean composition and registry edits without a rebuild.
The maintained latency instrument uses at least 6,500 pages, 45,000 chunks and 9,500 units on two CPUs.
Its budgets are 200 ms for hybrid, 100 ms for keyword, and diagnostic budgets of 6 ms for eligibility and 12 ms for units.
Unit tests establish correctness; the maintained before-and-after instrument establishes latency.

### Material title contact

The material lane retains a named page's title terms only within that resolved page's own admitted scope.
For each resolved subject request, its residual task may identify exactly one admitted material parent by title.
The lookup reads a separate `title_fts(stemmed)` projection with the existing tokenizer and title tokenization.
This adds one short indexed row per page through the common lexical builders; BM25 text and statistics stay unchanged.
Lexical schema 20 and owner generation 5, both unshipped, include this projection.

Title postings narrow the existing per-request scope before candidate-local visibility, lifecycle and exclusions decide which matches count.
Original `TokenUnit` values retain their run flags and strict-majority contact thresholds before the query's limit of two.
The lookup discovers no full admitted corpus, computes no ranking statistics and parses no page source.
Zero contacts add nothing, one permits expansion, and two remain ambiguous.
Unavailable or interrupted lookup remains visible through existing lane diagnostics while body matches remain usable.
Subject-only requests provide no residual title terms.

A unique parent enters the existing wildcard semantic-unit query, selected interpretation, category exclusions and reader hydration.
Body hits keep their order; fallback units append in source order and deduplicate by unit reference.
The request keeps the actual title witness separately from unit facts for allocation coverage.
The fallback manufactures no text, relations or role exemption, and keeps the normal packet and request budgets.

Page writes, replay and deletion maintain the title row atomically with the page's other derivatives.
Content custody compares exact expected title bytes through `_corpus_custody_verify` and `holds_content_identities`.
Offline store verification joins `title_fts` to `pages` through the existing full-text inventory; background reconciliation checks missing rows and orphans.
Copy reconciliation deliberately retains unchanged values rather than revalidating every byte.
Recall consumes the published schema/checkpoint witness and performs no verification scan or build.
Arbitrary SQL corruption after a valid publication is outside this transactional write guarantee.

The design applies the same guidance to indexed tool contracts and minimum sufficient request context.
The proof follows Anthropic's [Demystifying evals for AI agents (2026-01-09)](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents), keeping trials, traces and outcomes distinct.
The load target and budgets above still apply; the programme owner runs that latency proof after the coupled source batch settles.

### Stored family eligibility before ranking

The canonical-store path will propose occurrences through endpoint and dependency indexes, then decide selected family membership before ranking.
The original filter remains the exact predicate.
Candidate SQL preserves three-valued logic, so a possible edge cannot become an unsafe exclusion under negation.
File-mode family limits remain until this stored path has qualified inputs and delivery evidence.

The graph owner adds neutral authored inputs: raw targets on resolver-dependent edges and ordinary-body-link presence on dependencies.
The existing canonical-line exclusion distinguishes ordinary links from authored relation bullets.
Unresolved links need dependency presence because they have no stored edge.
The shared dependency keys include the resolver's title normalization, alongside existing path keys.
An owner-generation pass derives these inputs; missing or partial inputs warm only dependent family filters.

Explicit candidate batches prepare graph membership immediately before exact filtering.
Ranking-statistics transport does not prepare dimensions: it can traverse every admitted parent before a request has query candidates.
The admitted BM25 branch uses the same candidate preparation operation as other lexical and vector eligibility.
Endpoint-first adjacency and candidate-name dependency lookups cover links revealed when a competing page is withheld.
Neither a resolver walk nor a global family-edge list belongs on this path.

Selected summaries and neutral dependencies supply authored targets without parsing an unreturned page.
The producer and reader share target resolution, with destination existence and target summaries prefetched in batches.
Known endpoint paths survive that operation; per-edge node or existence queries do not.
The authoring registry decides terminal replacement and family before dimension folding, preserving private-instance meaning.
Raw-label proposals include every supported registry spelling, including explicit core names.
The iterator preserves selected fragments, page and unit endpoints, endpoint admission, and self-edge exclusions.

One snapshot supplies registries, selected summaries and graph inputs for the request.
Each candidate batch folds its visible edges directly into membership maps.
Ranking workers receive immutable eligible identities, never a live graph view or connection.
Lexical and vector lanes consume the same exact population.
The proof includes authored-definition expectations because the existing scan fold can share an instance-meaning defect.
Unrelated-data growth must not increase graph work for a fixed candidate batch.

### Indexed target proofs for stored family eligibility

Family eligibility must interpret each relation through its author's selected registry before candidate caps. It must not acquire or copy the complete resolver namespace.

Reuse the existing wikilink normalizer through a pure internal selector. Keep the public tuple API, exhaustive resolver diagnostics and legacy fallbacks unchanged. The internal result distinguishes resolved, ambiguous, missing and incomplete. Prepared lookups perform no SQL, filesystem reads or global resolver acquisition.

Maintain one derived `wikilink_keys` projection with physical path, normalized path key, stem key and nullable title key. Three `(key, path)` indexes support staged lookups. `pages` remains namespace and metadata authority, including admitted title-null and unparseable physical rows. Extract one pure key helper shared with the full resolver; preserve its case, extension and Unicode rules. This costs one short row and three indexes per page. Existing title FTS has different matching semantics. Expression indexes would require deterministic functions on every writer connection and matching SQL expressions ([SQLite expression indexes](https://www.sqlite.org/expridx.html)).

Resolve requested targets in batches by precedence. Exact-path success must not load stem or title collision buckets. Batch metadata and admission before counting distinct physical owners. Exhaustion proves zero or one owner; two admitted witnesses prove ambiguity without claiming an exact count. Incomplete zero/one-owner searches remain unknown, including under negation. Deadline exhaustion propagates through the existing unavailable outcome. Resolve fragments from stored selected structures on the same snapshot. Consume typed outcomes before graph landing; never swallow lookup requests in broad exception handling or interpret warning prose.

The family operation supplies no retired-alias fallback. A retired alias names a removed item file, while family edges require ordinarily admitted, existing endpoint nodes. An alias reached only after ordinary namespace resolution fails cannot supply such an endpoint. Preserve public alias behavior in the legacy wrapper. Keep access-policy filesystem fallbacks there too.

Common page writes, deletion, replay and cold verification maintain the projection. Fold it into the readiness of unshipped lexical schema 20 and owner generation 5. Released-owner writers must tolerate its absence through explicit transaction-cached capabilities. Future key-normalization changes invalidate the existing derived generation; add no separate job or attestation.

Prove full-resolver parity, lower-bound ambiguity diagnostics, hidden collision admission, incomplete negation, legacy-owner writes and cold parity. Prove author-selected private-family membership through public results before caps. Check retired aliases at the real endpoint boundary. Reuse the maintained load instrument for the recorded keyword and hybrid budgets.

This applies the primary-source roster's [context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents), [tool design](https://www.anthropic.com/engineering/writing-tools-for-agents) and [outcome evaluation](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) guidance.

### Review queues by dimension

Review listings expose the dimensions of the semantic unit or page each item concerns.
One request-local adapter batches selected parent structures and stored family membership through the existing readers.
It returns annotations and eligibility; each queue retains ranking, state, identity, coverage and surfacing.
This adds no tables, persistent cache, resolver walk or paging framework.

Each queue supplies its target explicitly.
An explicit page-scoped reason selects the page.
Otherwise, unanimous resolved unit reasons select that unit; conflicting or missing unit addresses remain unknown.
A missing address on a partitioned unit finding does not silently select the page.
Producers bind unit addresses to the same parent identity as selected structures, including stable memory references on ID-bearing pages.
Relation suggestions concern their author page, preserving `from` and `target_ref`.
Upkeep concerns its subject, not its evidence pages.
Procedure approvals concern uniquely resolved procedure pages.
Vocabulary items use an explicit semantic page when one exists.
Import proposals, planning intents and registry overlays have no semantic subject and are not applicable.
Queue adapters use producer fields, never category names or warning prose, to determine target scope.

The public `list_review_items` tool accepts optional `filters` through its existing reader route.
Compile the shared structured-filter grammar once and reject predicates outside `unit.dimension` and Boolean composition.
Display may union dimensions across selected units.
Filtering uses the existing same-unit existential rule: one target unit must satisfy the complete expression.
It never evaluates the display union as a synthetic unit.
A complete page without units has known-empty display membership but matches no unit predicate, including negation.
Not-applicable items likewise match no dimension predicate.

Each item reports dimension status as known, unknown or not applicable.
Only known membership carries `dimensions`, including an authoritative empty list.
Unknown membership omits that list and identifies the unavailable dependency in diagnostics.
Unavailable definitions, incomplete structures, unavailable required edges and unresolved unit addresses remain unknown, including under negation.
An unfiltered listing retains such items; optional annotation cannot make the whole queue refuse.
Filtered output keeps known matches and reports unresolved classifications separately.
Admission precedes target preparation and counting; withheld candidates cannot affect dimension counts.
Filter eligibility and optional annotation completeness are separate outputs of the same adapter.
A kind-only filter can return an exact match while unavailable edges leave full annotation unknown.
Only dependencies consumed by the filter can make its classification unresolved; missing target addresses or selected definitions remain relevant to every dependent filter.
Coverage reports filter matches and unresolved classifications separately from known and unknown annotations.
Each batch reads filter-required families first through the shared deadline-aware edge reader and records exact filter truth.
Optional annotation reads only the remaining families under the same pinned snapshot; it never repeats required-family work.
An interrupted optional batch leaves the recorded match intact and full annotation unknown.

For a positive attention limit, the existing ranked, state-eligible prefix becomes the dimension candidate window.
Evaluate that window before final selection and surfacing; record surfacing only for returned items.
Underfilled output is allowed, with partial coverage when eligible items remain beyond the prefix.
Internal uncapped identity lookup does not acquire dimension annotations.
Explicit uncapped listings prepare batches under the existing request deadline; unprepared items remain unknown and coverage remains partial.
No new overfetch constant, recursive refill or silent positive cap is introduced.

Procedure approvals retain their existing reference prefix as an upstream window, avoiding graph preparation across the entire approval ledger.
Relation review applies eligibility before its final group/page break, preserving existing source and branch windows.
Vocabulary review uses its original visible prefix of `limit` items as the dimension window.
It authorizes that same prefix through the existing wrapper rule before preparing dimensions, preserving the registry-proposal exception.
It batches dimensions inside that authorized window before returning items; underfill remains partial.
Its continuation retains candidate order and the unfiltered tail; changing filters cannot silently change the continuation's meaning.
Other queues preserve their existing upstream windows and apply dimension selection before their final output slice.

Existing totals retain their upstream meaning.
Dimension coverage names the evaluated window, known matches, unresolved items and unsearched remainder separately.
An empty partial result means no known matches in that window, not no matching review work.
Replace completeness prose whenever it would describe only a filtered prefix.
Attach dimension annotations after identity computation, outside reasons and fingerprints.
A registry edit can change display membership without reopening a recorded review decision.

Reuse selected-parent adapters, stored family preparation and structured-filter evaluation.
Custom code supplies queue target adaptation and availability reporting; a recursive result decorator would filter after caps and record excluded surfacing.
The recorded 6,500-page reference and public warm-call p95 target in `request_latency_contract.json` apply to the added work.
Measure before and after on the same instrument; report inherited producer scans separately as existing debt.
Proof covers mixed reason scopes, same-unit Boolean expressions, unknown negation, bounded windows, disclosure, continuation and unchanged review identities.
Real-model evidence must show the item field helps before this surface ships.
The context, tool-design and outcome-evaluation primary sources cited above govern that proof.

### Degree of belief, owned by its holder

The owner chose this shape on 2026-11-08: belief levels, plus numbers only with a source. The owner added that Exomem must not arbitrate belief.

1. A belief is a unit of the new kind `belief`. It states the belief in prose, so it is never an empty unit. Its governed keys are `credence_holder` (a plain name or a link to an entity page), `credence` (the value) and `credence_basis` (free text).
2. The unit points at the claim it assesses through the new core relation `assesses`, family `assessment`. Validation finds that edge through its behaviour `names_assessed_claim`, so a vault extension of `assesses` serves too. A belief unit without it warns in the first release.
3. The value is a key or label of the belief-level registry (speculative, plausible, likely and near-certain by default), or a probability from 0 to 1.
4. Every belief names a holder. A probability also needs a basis, and the basis can be anything the holder gives, including intuition.
5. Exomem keeps the stated value as written. A label resolves to its level key for matching, but the stored text is unchanged, and a probability is never rounded.
6. Several holders' beliefs about one claim sit side by side, and none replaces another.
7. A `credence`, `credence_holder` or `credence_basis` row on another kind is an error that shows the belief-unit form. The likely misuse is `- credence: 0.8` on a claim.

A later change can score calibration from a probability, the assessed prediction's `check_by` and its verdict. This change only keeps the data that needs.

`credence_holder` names whose belief it is. `asserted_by` names whose claim a unit is. They are different keys, because the two questions differ: a vendor asserts an uptime figure, and the team believes it is likely wrong.

Exomem writes no belief in this change. A later change that computes one must label it derived and must not replace a holder's belief.

The rejected alternatives were levels only, which keeps the old rule but gives up calibration, and free numbers on any claim, which is the most exposed to unsupported precision.

### New governed metadata keys

- `asserted_by` is a plain name or a link to an entity page. Context presents a unit with it as that party's claim.
- `modality` takes a modality-registry key: is, should, must, may. Retrieval and context never merge units whose modalities differ.
- `occurred_at` is a date or a date and time on a unit with the `dated_event` trait, which ships on `timeline_event`.
- `quantity_value`, `quantity_unit` and `quantity_error` record one measured quantity. Records remain the place for a series.
- `inference_mode` takes an inference-mode registry key on a unit with the `assertion` trait.

Every new key name is chosen to avoid ordinary rows: `value`, `at`, `holder`, `basis` and `mood` are common words in existing notes, and `credence`, `occurred_at` and `modality` are not.

`credence` is a unit row on a belief unit, never a frontmatter field. The canonical rule that confidence is not a frontmatter field still holds. Today's teaching says Exomem keeps no credence or numeric confidence anywhere. Slice 4 rewrites `judge_categorically` in `commands.py`. Slice 6 rewrites the other sites: the scaffold's frontmatter, skill and vault-care references, and the comment beside the verdict set in `semantic_units.py`.

Each new key is reserved, as `verdict` and `check_by` are. A page that already uses one as an ordinary row gets a warning, not an error, for one release. The warning names the repair: rename the row, or give it a valid value.

Task 4.5 ends this migration period. Invalid or misplaced rows, incomplete beliefs and missing assessed-claim relations become errors with the existing repairs. Metadata-only rich units leave the parsed units, stored structural views and search results. Parser version 9 invalidates version 8 summaries, whose stored `substantive_body` could retain an empty unit. The existing freshness and reconcile paths rebuild these summaries; this transition adds no state schema or repair service.

### Multilingual labels

Each registry entry carries `labels: {language: text}`. Keys stay language-neutral. Parsing accepts a label as an alias of its key, so `## Väite` and `## Claim` are one kind. The registry check refuses a label that collides with another entry's key or label.

`observe_memory` uses `selected_parent_index_state` for lookup, returned units and page short IDs.
Kind and verdict validation read that selection's categories snapshot, with the existing project scope.
The parent index and lexical sidecars keep neutral structure; registry changes need no corpus rebuild.
If definitions become unavailable, dependent unit resolution reports unavailable and the complete unit list is unavailable.
Core-safe units retain the selected adapter's existing read and mutation contract.

This preserves the canonical semantic-write contract and `add-governed-vault-consolidation`.

## Debt

Each item stays as named debt, with its reason in a code comment (task 2.4).

| Item | Sites | Why it stays |
|---|---|---|
| F7 same-page epistemic flag | relation_census 76, 1460 | Census-only. It is family-keyed, so extensions already reach it. |
| Projected graph lane tier (K16, change 6) | projection_runtime 1825 | A stored projection edge keeps its canonical key and no extension parent, so only a core plain link ranks last. An extension of `links_to` ranks as typed here until the producer stores the parent. |
| Shared-source copy, drifted from the graph copy (K14 fold) | relation_queue 700; epistemic_graph 14156 | The two copies disagree. The relation queue reads the frontmatter field's stored edges only. The graph copy also reads page-level extensions, and it reads the binding's relation from any origin. Folding them changes suggestions and needs its own evidence. Both read the field and relation from the binding. |
| F8 affiliation count | relation_census 1232 | One entity-census count for one family. |
| K7 evidence-target check, with C8 `_EVIDENTIAL_KINDS` | relation_census 74, 1447 | Census-only. `target_kinds` cannot express it, because folders and Sources pages count. The kind list stays with the check. |
| K8 answers-target check (relation side) | relation_census 1454 | Census-only. `settles` is wrong here, because a `resolves` target is often not a question. The target side reads the `question` trait. |
| K18 asserted-contradiction label | audit 7057 | A label on the finding. The pairs come from `opposes`, and an extension is labelled by its parent. |
| K19 duplicate default | adoption_proposals 448, 1327 | A producer default that writes one core key. A vault cannot remove a core key. |
| Wikilink producers and their authored-match checks | semantic_contract 4645; epistemic_graph 9046, 9312, 12361, 14135, 14923; relation_queue 661; governance/graph_producer 79 | Producer constants. `origins: [wikilink]` already states the fact. Convert them through one `origin_relation()` accessor when a second core relation lists that origin. |
| K21 pointer readers | find_types 202, 209; working_set 480, 1163, 2671; stance 77; delete_file 230; replace 436; context_pack 365, 673; memory_context 135; audit 6229, 6370; semantic_contract 4805; working_set_currency 34 | Naming the field tokens is sound. The repeated rule "a successor pointer means not current" is debt. Fold it into one lifecycle predicate that reads the reverse field from the `supersedes` binding. |
| K22 field-name lists | lexstore 584; episode_capture 54; review_context 37; entity_types 76; governance/bridges 60; governance/egress 812, 856, 5138; working_set_conversation 143; memory_context 136 | Transport and allow-lists. They carry no behaviour. |
| C4 and C5 category fallbacks; C6 category leg; C9; C11; `CONCLUSION_CATEGORIES`; `_CATEGORY_BY_LABEL` | artifact_role_review 84-92; epistemic_graph 14748; audit 6018; context_roles 796; working_set_index 291 | Category registry, out of scope per the proposal. C9 stays on category for the reason in inventory decision 5. |
| C10 `UNITS_V1` | sensing 245-275 | Frozen instrument (proposal). |
| C12 `RECOGNIZED_SECTIONS`; C13 `PACK_ALLOWED_PRIMITIVES` | context_pack 101; knowledge_packs 62 | Heading text (proposal). C13 is a closed manifest schema that mixes page types and kinds. |
| Neutral sidecars and selected vocabulary | semantic_index `current_parent_index_state`; lexstore `unit.verdict` column | These store neutral structure by contract. Selected readers and `observe_memory` interpret each admitted page with its definitions; a registry change needs no rebuild. |
| A `Relations` heading that names a kind | semantic_blocks relation section; semantic_language_registry `_check_kind_labels` | A vault kind's heading alias of `Relations` turns every page's `## Relations` section into a unit of that kind, and a label on a shipped kind can now do the same. The registry check does not refuse that spelling yet. |
| Page-type keys found on the way | vocabulary_projection 78; dreamer_families 615; artifact_role_state 207 | Note-type keys where the note-type `role` states the fact. Outside this change. |
| Retry advice while the graph is stale | find `_resolve_relation_filter`, `_dimension_edges` (warming site `relation_graph_rebuilding`) | On the scan path, a `unit.dimension` filter that needs edges, or a `relations=` filter, raises the warming outcome while the graph rebuilds, and its remediation says to retry. After a direct file edit, only a reconcile (`maintain_memory` with mode `reconcile`) ends that state, so a retry can return the same outcome. The remediation belongs to the shared warming outcome, so this slice does not change it. |
| Two rules for an answered question | audit `_bears_on_unit` (question aging); dimension_display `flag_open` | The audit's question-aging check treats a question as answered by its own outbound `bears_on` relation. `flag_open` treats a unit as settled by an inbound edge whose relation carries `settles`. The two rules can disagree about one question. Folding them changes audit findings and needs its own evidence, so this change does not alter the audit. |
| Two rules for a replaced unit | dimension_display `page_is_history`; working_set_currency `state` | `recall_context` treats a unit whose `until` date has passed as replaced, and the dimension view does not: a group marks a unit replaced only when its page's lifecycle class is history or an edge whose relation carries `replaces` ends at it. Folding the expiry into the view changes which units a group leads with, so it waits for the working-set slice (task 5.2). |

Sound, not debt: K23 (NLI output labels, a closed model schema), the `generic_relation_key()` readers, projection_runtime 1837 (a core-or-extension test), and K24. Bootstrap now teaches its kinds and relations from each shipped entry's `teach` line, and the `relation_vocabulary` plain-link sentence reads its key from the registry.

## Intended behaviour changes

Each line needs a fixture case.

Extensions, through the parent:
1. An extension of `supports`, `contradicts`, `resolves` or `evidenced_by` clears a due prediction or open question (K2).
2. An extension of `derived_from`, `evidenced_by` or `cites` counts as activation provenance (K3), and entity hydration skips it (K12).
3. An extension of `answers` or `resolves` feeds `shared_resolution_target` candidates (K1).
4. An extension of `supersedes` reaches three readers: role review collects its target (K5), the census builds its chain and date check (K6), and the sensed model records it (K11).
5. The census checks an extension of `contradicts` inside a supersession chain (K9). Stance and the audit already reach it.
6. An extension of `links_to` ranks as a plain link in referent resolution and the vocabulary pair query, and the census skips it (K16 readers, F10 key leg). The graph's `typed` already treats it this way. The projected graph lane is excluded (see Debt).
7. An extension or alias of `about_entity` names the subject in restricted working-set admission (K20).
8. A page-level extension of `derived_from` seeds shared-source candidates. It also stops a frontmatter source from being proposed again as an authored relation (K14, through the binding).
9. A grandfathered extension whose saved family differs from its parent's follows the parent in every converted family read: lifts, corrects, associative, membership, replaces, plain_link.

Widened sets, core relations:

10. Stance treats two pages that both `resolves` one target as a declared pair. Near-duplicate and overlap warnings stop for that pair (K17).
11. A `cites` or `evidenced_by` edge into an entity page stops counting as a content link. That covers hydration targets, contributors and authored links (K12).

Kinds:

12. A vault kind with the `question` trait joins shared-open-question candidates and satisfies the census `answers` target check. The census drops its literal `question` (C7). The fixture shows that the plural and synonym headings still resolve to `open_question`.

## Fixture

Tasks 2.6 and 3.2 use one fixture vault, and compare main's code with the converted code.

Core parity: these outputs must be identical.
- `suggest_relations`: `unit_relation_lift`, `shared_resolution_target`, `shared_open_question`, frontmatter-source and shared-source candidates. Also the review batch's lift and source rows.
- Catalogue membership answers.
- Audit `prediction_window` and question aging.
- Activation counts (typed, provenance, assertion, frontmatter links) on a page that uses all seven frontmatter fields.
- `qualify_relation` reasons, including a frontmatter `superseded_by` pair and a mutual-supersession target.
- Unit-correction counterparts, and the `observe_memory` exact-target guard.
- Working-set currency.
- Role review: supersession targets, procedure and outcome.
- Sensed-model page facts.
- Census rows and every census check.
- Referent and projection tiers, for one plain link and one typed edge.
- The page claim.
- The built-in traversal profiles.
- For each frontmatter field, the graph and the relation facts record the same edge. Activation counts the relation per field and records no edge direction.

Extensions: one per parent, each showing its line in the intended-changes list.
- `x.backs` (supports), `x.based_on` (derived_from), `x.closes` (resolves), `x.retires` (supersedes), `x.disputes` (contradicts), `x.see_also` (links_to), `x.concerns` (about_entity).
- One grandfathered extension with parent `relates_to` and saved family `support`.
- Control: `x.qualifies` (refines) lifts before and after.

Widened sets:
- Two pages that both `resolves` one target.
- A compiled page that `cites` an entity page.

Kinds:
- A vault kind `research_question` with the trait `question`.
- A vault kind `protocol` with the trait `procedure`.

## Migration path

1. Land the registry plumbing (tasks 2.2 and 2.3) with no reader change. Record the fixture baseline on main first.
2. Convert the readers one module group at a time. Each conversion keeps the core parity outputs and flips exactly its own intended-change lines.
3. Release as one batch. Registry schema reads now show `behaviours` and `frontmatter_fields`, so regenerate the derived artifacts: the capabilities doc, the hosted render and the plugin tree.

## Risks

- **A behaviour change hides in a data migration.** For core relations, the shipped pack must reproduce today's sets, except two named widenings. The before-and-after fixture test guards each cutover.
- **Agents misuse the new words.** Each slice proves on real models that an agent picks the right word for realistic and hostile inputs.
- **Unsourced numbers creep in.** Validation refuses a probability without a holder and a basis, with an error that names the missing key.
- **Display annotations add noise.** `flag_open` marks every open question, hypothesis, prediction and assumption without a verdict. Slice 5 checks on real models that the annotations help, and f38's P arm shows their effect on outcomes.
- **Belief units add authoring weight.** A separate unit costs more than a row on the claim. In return, each belief keeps its holder, and several holders can disagree on record.
- **Behaviour keys from `core_registry()` alone miss vault extensions in SQL.** A stored key column holds core keys, and an extension is a candidate that each page interprets. Readers ask `exomem_edge_behaves`, or expand the keys through `relation_query_plan` over the admitted instances.
- **A non-core key means what its authoring page's instance defines.** A behaviour lookup uses that page's registry, as the census does.
- **The grandfathered family-mismatch case flips silently.** The fixture holds one such extension.
- **A partial cutover leaves one concept on mixed family and key reads.** The fixture asserts extension reach in every reader of a behaviour, not in one.
- **A later pack drops a shipped kind.** A vault's labels-only entry under that key then reads as a malformed vault kind. That is an error finding, so every vault kind and label stops resolving until the vault removes the entry.
- **Rollback.** A reader revert is a code revert. The pack attributes are additive, and old code ignores them. A vault overlay is not: older code reads a saved `verdicts:` section or a kind's `traits:` as `unknown_field` errors, so every vault category then resolves `registry_invalid`. Code before slice 4 does the same with a saved `belief_levels:`, `modalities:` or `inference_modes:` section, and reads the trait values `credence` and `dated_event` on a vault kind as `unknown_trait` errors. Rolling back after a vault saves any of these needs those sections or trait values removed from its registry first.


## Current-turn subject precedence

Automatic display evaluation exposed a shared resolver abstention before annotation. An unrelated weak candidate could block an exact subject name. The accepted repair changes the competition boundary for both evaluation arms. It changes no dimension membership, retrieval evidence or role selection.

Use existing categorical candidate facts and the current turn's name spans. A focus alias does not prove that the current turn named a subject. Keep unrelated weak alternatives as partial leads. Preserve structurally complementary context. Check ambiguity within each group of overlapping current-turn names. A separately named topic cannot bridge competing interpretations of another name. Preserve agent identity choices and existing behavior without a current-turn name.

The existing resolver supplies candidate facts, spelling spans and structural membership buckets. No external resolver fits this internal evidence contract without replacing those components. Reuse those components; the added maintenance is one categorical competition rule and its regression boundaries. This adds no catalogue query or model call.

The context-engineering and tool-evaluation guidance supports this bounded boundary change: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents and https://www.anthropic.com/engineering/writing-tools-for-agents. Deterministic resolver and activation checks establish the rule. The real-model display workflow remains the release acceptance check. A passing resolver check cannot establish an annotation benefit.
