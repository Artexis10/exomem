## Context

Exomem already parses governed heading-sized semantic blocks, indexes those blocks as graph nodes, validates typed relations through a governed registry, and profiles recurring fields/blocks/relations through `schema_memory`. Normal recall still returns pages, however, and ordinary writers do not enforce saved memory contracts. A lightweight fact such as a configuration value, rule, term, preference, or todo therefore has no compact, category-addressable, independently retrievable representation.

Basic Memory demonstrates the value of a small Markdown grammar: a note entity contains `- [category] content` observations and typed relation bullets, and observation records can be filtered by exact category. Its categories and relation labels are open vocabulary. Exomem's advantage is a deeper governed substrate—stable memory identity, Sources/Evidence separation, semantic-block relations, raw/canonical relation identity, provenance, supersession, review, and traversal lenses—but the product is not superior while the compact observation and per-note schema workflows remain missing.

This change treats compact observations and rich semantic blocks as two authoring forms of one semantic-unit language. Markdown remains canonical. Every index is derived and rebuildable. Parsing, lexical filtering, graph construction, and schema validation are deterministic; optional embeddings remain measurement-only and soft-fail to lexical behavior.

The change also completes the previously identified relation-contract gap. A semantic language is only dependable if every write/edit/reconcile path applies one shared contract instead of treating relations and units as advisory formatting.

## Goals / Non-Goals

**Goals:**

- Match the useful Basic Memory observation grammar closely enough that existing `- [category] ...` notes are immediately understood.
- Exceed that model with governed epistemic kinds, stable parent identity, addressable anchors, provenance, lifecycle state, typed block relations, schema governance, and review.
- Make semantic units first-class in recall and tooling without changing the default page-level result contract.
- Make page frontmatter and semantic-unit metadata queryable through one safe structured filter language, including filter-only retrieval.
- Make ranking inspectable on demand without overloading one unlabeled score or bloating the default compact response.
- Keep categories open and ergonomic while making their raw/canonical identity, aliases, deprecation, scope, and contract use reviewable.
- Apply one semantic contract to all in-process writers and surface out-of-band violations without destroying user edits.
- Prove shared local-core parity, expose any remaining gaps, and demonstrate only recorded Exomem extensions through isolated end-to-end fixtures against the sibling Basic Memory checkout.
- Preserve empty-vault onboarding and existing-vault adoption without bulk rewriting Markdown.

**Non-Goals:**

- Hosting, sync, collaboration, billing, web editing, or other cloud-product breadth.
- A server-side reasoning or ontology-inference model.
- Automatically deciding whether a statement is true, authoritative, contradictory, or semantically related.
- Requiring every paragraph or bullet to become a semantic unit.
- Rewriting existing notes into compact observations or adding visible IDs in bulk.
- Making every category a global governed enum or silently mapping domain labels such as `term`, `rule`, or `config` onto epistemic kinds.
- Replacing page-level retrieval, semantic blocks, canonical note relations, or Markdown with a database-owned object model.
- Arbitrary SQL, regular expressions, executable predicates, or unbounded user-defined filter code.
- Treating BM25 magnitudes, cosine similarity, reciprocal-rank-fusion values, or reranker scores as interchangeable confidence values.

## Decisions

### One normalized semantic-unit model, two authoring forms

The engine will expose one immutable parsed `SemanticUnit` shape with:

- parent memory identity and current path;
- `unit_ref`, source anchor/span, source hash, and authoring form;
- governed `kind`;
- `category_raw`, immutable authored `category_key`, and registry-resolved `category` (defaulting to the key);
- content, context, and tags;
- authored relations and inherited/explicit provenance;
- parent lifecycle status and registry/contract findings.

Compact observations use normal list syntax:

```markdown
## Observations
- [config] Session lifetime is 30 days #auth
- [rule] Refresh only after access-token expiry #auth (security review)
- [term] Durable session means OS-backed refresh state
```

The parser accepts this form anywhere outside fenced code for Basic Memory compatibility, while writers place new compact units under a canonical `## Observations` section. After trimming, a category starts with a Unicode letter and contains only Unicode letters/digits, spaces, `_`, or `-` (64 Unicode code points maximum). That deliberately excludes Markdown task boxes (`[ ]`, `[x]`, `[X]`, and `[-]`), Exomem's existing `[take: ]` review rows, and bracketed workflow prose. Category text is preserved raw; its authored canonical form is Unicode NFKC + casefold with runs of spaces, `_`, and `-` collapsed to `_`. Registry aliases are resolved separately and never alter authored identity.

Suffix parsing is deterministic. After optional final `^anchor`, the parser removes one final balanced, unescaped parenthesized context preceded by whitespace, then a contiguous run of trailing `#slug` tokens. A tag slug is 1–64 Unicode letters/digits or `_`, `-`, `/`, begins with a letter/digit, and has neither empty path segments nor a trailing `/`. A compact anchor is 1–64 ASCII letters/digits/hyphens and begins/ends alphanumeric. Escaped parentheses, embedded hashes, and non-trailing tag-like text remain content. Anchors use Obsidian's terminal block-anchor position and must be unique across all compact and rich units in one page. Raw category spellings that normalize to the same authored canonical value form one exact-filter union and remain visible in schema profiles; conflicting registry aliases remain a validation error.

Existing semantic blocks are the rich form. A block's governed heading remains its `kind`. Its category defaults to the kind and may be overridden with leading `- category: ...` metadata. Thus `categories=["decision"]` finds both `[decision]` compact observations and ordinary `## Decision` blocks, while `kinds=["decision"]` selects only units carrying the governed decision kind. A block may carry the existing metadata and typed relations; a compact observation deliberately stays small.

Alternatives rejected:

1. Adding `category` metadata only to rich blocks preserves governance but misses the one-line authoring and retrieval advantage.
2. Cloning Basic Memory observations into a separate database model creates two competing semantic systems and loses Exomem's block/provenance lifecycle.
3. Expanding the built-in block enum with every observed label turns domain vocabulary into code and repeats the relation-ontology fragmentation problem.

### Open categories with raw/canonical identity and optional governance

Categories are open by default. Unknown categories are valid and searchable. The parser preserves `category_raw` and emits an authored canonical `category_key`; registry resolution emits `category`, defaulting to that key. It never assigns a stronger epistemic meaning, and identity never depends on mutable alias resolution.

`schema_memory(subject="categories")` will profile category frequency, forms, examples, page/project scopes, and alias candidates without mutating the vault. Explicitly saving a reviewed proposal may register aliases, deprecation/replacement, and scopes. Saved page/project contracts can require categories or restrict unknown categories. This follows the relation-registry pattern while keeping casual authoring frictionless.

The category registry and semantic-block kind registry share a small semantic-language registry document under `_Schema/`, but categories and kinds remain separate namespaces. Built-in kinds remain portable defaults. A knowledge pack or reviewed registry extension may add recognized rich kinds without a code release, fulfilling the existing registry-driven vocabulary direction.

### Stable when authored, fingerprint-bound when anonymous

Rich blocks retain their current optional `- id:` anchor. Compact observations may use a standard Obsidian block anchor (`^anchor`) at the end of the list item. Tool-authored updates that need durable unit references add a compact anchor; ordinary hand-authored units do not require one.

An anchored unit's durable reference is its parent `exomem://memory/<uuid>` plus anchor. Duplicate authored anchors—whether compact, rich, or cross-form—are rejected. An anonymous unit receives a derived reference from parent identity, authoring form, normalized authored/raw category, explicitly authored kind/content/tags/context/relation metadata, and its source-order occurrence among identical authored signatures. It survives a page move only when the parent has a stable `exomem_id`, and intentionally changes when its authored semantic content changes. Inserting or removing an earlier identical anonymous occurrence may invalidate later duplicate references; the returned fingerprint and span make that limitation explicit. Registry alias/canonical changes do not change unit identity. Mutation tools require the current unit fingerprint/parent content hash, so stale anonymous references fail safely instead of editing the wrong bullet.

Legacy parent pages without an `exomem_id` use the existing path-based fallback, so their anchored and anonymous references change on move. They receive the same audit/backfill guidance as other durable-reference consumers. No identity is silently written during reads or indexing.

### A dedicated parser feeds every derived consumer

A new focused semantic-unit module composes the compact-observation parser with `semantic_blocks`; it is the only normalization boundary. Writers, write feedback, schema inference, lexical/vector indexing, graph construction, context packs, audits, and benchmarks consume its result rather than reparsing Markdown independently.

The parse result distinguishes errors from warnings and includes spans. Malformed observation-like bullets remain ordinary Markdown plus a diagnostic; they are never partially indexed as valid units. Unknown headings remain ordinary Markdown. Optional embeddings receive unit content only after deterministic parsing and use the existing soft-fail policy.

### First-class retrieval is additive and explicit

`find`/`ask_memory` gain:

- `result_level`: `auto` (default), `page`, `unit`, or `mixed`;
- `categories`: exact registry-resolved category filters, OR within the list;
- `kinds`: exact governed kind filters, OR within the list;
- `filters`: a bounded namespaced structured expression over page and unit metadata;
- `explain`: opt-in retrieval-plan and per-hit ranking evidence, default `false`.

With no unit filters, `auto` resolves to `page` and preserves the existing result bytes and ordering. Passing `categories` or `kinds` makes `auto` resolve to `unit`. Values are ORed within each filter list, while text, category, and kind axes are ANDed when supplied together. `page` with unit filters returns parent pages whose units match and carries a bounded `matched_units` annotation. `unit` returns independently ranked semantic-unit hits. `mixed` fuses page and unit candidates, caps repeated units per parent, and preserves explicit result identity.

Unit hits carry `result_type="semantic_unit"`, unit/category/kind fields, parent path/reference/title/type/status, anchor/span, excerpt/content, and ranking signals. Category-only and structured-filter-only search work with an empty text query. Exact filters apply before ranking, so text mentioning the word `decision` cannot satisfy `categories=["decision"]` unless the parsed unit is actually categorized `decision`.

Lexical unit records live in the existing lexical sidecar with a record discriminator. Optional unit embeddings live in the existing embedding sidecar with a unit key and parent path. The epistemic graph adds compact unit nodes and `derived_from` edges; authored rich-block relations retain their current behavior and existing rich-block graph node keys. One normalized rich unit produces one graph node/edge identity. The legacy `semantic_blocks` context field becomes a bounded compatibility projection from `semantic_units`; it does not trigger a second parse, row set, graph node, or duplicate result. A schema-version bump triggers rebuild rather than Markdown migration.

### One safe filter language spans pages and semantic units

`filters` is a JSON object whose field predicates use explicit namespaces. Reserved system metadata uses fixed names such as `page.status`, `page.project`, `page.updated`, and `page.file_type`. Arbitrary frontmatter uses `page.frontmatter:/<RFC-6901-pointer>`, so nested mapping keys and literal `/` or `~` characters remain unambiguous (`page.frontmatter:/priority`, `page.frontmatter:/vendor/id`, `~1` for `/`, and `~0` for `~`). Mappings are traversable and support `$exists` but are not equality-compared. Arrays are terminal scalar collections: if an array is encountered before the pointer is exhausted, that candidate is a nonmatch; the same numeric segment remains a valid mapping key when the runtime value is a mapping. `unit.<field>` addresses the closed semantic-unit metadata set: `category`, `category_key`, `kind`, `tags`, `context`, and `form`. Unit results evaluate the complete expression against one `(parent page, unit)` pair. Page results evaluate it existentially over one child unit at a time, so category and tag predicates cannot be satisfied by different units; a page-only logical branch may still match a page with no units through a missing-unit sentinel. A page-only expression keeps `result_level="auto"` at page level, while any `unit.*` predicate makes `auto` resolve to unit level, like `categories` and `kinds`.

Leaf predicates support `$eq`, `$ne`, `$in`, `$all`, `$contains`, `$exists`, `$gt`, `$gte`, `$lt`, `$lte`, and inclusive `$between`. Multiple operators on one field are ANDed. `$eq`/`$ne` accept scalar or null operands and compare only scalar/null fields with exact type identity; a runtime array or mapping is a nonmatch, and using them on a closed field known to be an array is a validation error. Arrays use `$in` for scalar membership/array overlap, `$contains` for exact scalar membership, and `$all` for requiring every requested scalar, with operand order and duplicates semantically irrelevant. `$contains` on an explicitly string-valued field means exact substring. `$in`/`$all` and logical lists must be non-empty, `$between` has exactly two ordered values, and `$not` has exactly one child expression. Ordered comparison is allowed only for scalar numbers, ISO date values, or timezone-qualified RFC 3339 date-times; date-times normalize to UTC, and date/date-time types do not mix. Values are not silently coerced between strings, numbers, booleans, and dates. Comparisons against a missing field or incompatible runtime type are false—including `$ne`—except `$exists:false`; logical `$not` is the actual complement and may therefore include missing fields. Logical `$and`, `$or`, and `$not` compose predicates with maximum nesting depth four and at most 32 leaf clauses.

Resource bounds apply before candidate work: the encoded `filters` JSON and both structurally normalized and alias-resolved combined filter plans (generic expression plus shortcuts) are each at most 16 KiB; each RFC 6901 pointer is at most 512 UTF-8 bytes and 16 decoded segments; each string operand or shortcut value is at most 1,024 Unicode code points and 4,096 UTF-8 bytes; each `$in`/`$all` operand and each shortcut list is at most 64 scalar values; all scalar collection and shortcut values together carry at most 256 values; and numeric operands must be finite JSON numbers with at most 64 encoded characters. Raw counts apply before deduplication or alias resolution; raw/structural bounds are checked before alias resolution, resolved bounds again before backend access, and the bounded resolved combined plan is the only filter payload echoed by `explain=true`. Regex, arbitrary SQL, and executable expressions are rejected.

Existing typed shortcuts (`types`, `projects`, `tags`, `speakers`, file types, dates, `categories`, and `kinds`) remain stable and compile into the same normalized filter plan. Shortcut lists retain their documented OR-within-list behavior; independent shortcut/filter/query axes combine with AND. Reserved system/unit fields use the same canonicalizers and case behavior as their shortcuts; arbitrary frontmatter strings compare exactly after YAML parsing. Category aliases resolve before comparison, status/type/date use their canonical typed representations, and invalid paths/operators/value types fail with a path-addressed validation error rather than producing an empty result. Access scope and excluded-subtree rules run before caller filters and cannot be weakened by an expression. Filters then run before candidate ranking in every retrieval lane and identically across SQLite and optional backends. Empty query plus filters returns filtered-most-recent results at the resolved result level.

### Retrieval explanations are opt-in, exact, and mode-safe

The default remains `explain=false`. Existing `detail="compact"` and `detail="full"` responses keep their current bytes and ordering when explanation is omitted. `explain=true` is orthogonal to detail: it adds bounded diagnostic objects but does not add note bodies, excerpts, semantic-unit content, or other full-detail fields on its own.

The top-level `retrieval_profile` records an explanation schema version, the resolved intent, result level, requested/effective modes, normalized filter plan, available/degraded lanes and reasons, lane weights, fusion algorithm/constants when fusion ran, rerank decision, and final ordering/tie-break policy. Each lane identifies its backend or model where relevant, metric name, better-direction/range, and rounding. Each hit gains `ranking_explanation` containing only lanes in which it participated: lane rank; metric name and value where one exists; reciprocal-rank-fusion contribution only when fusion ran; graph seed/relation/direction/hop provenance; applied type/status/recency/usage multipliers; reranker raw and adjusted value when used; the actual final sortable tuple; and final rank. Unavailable lanes and available lanes that did not return a given hit are represented only in the top-level profile, never by a fabricated per-hit entry or zero.

BM25 explanation carries backend name, rank, raw backend score, and score direction labelled diagnostic and non-comparable across corpora/backends. Vector and CLIP measurements are labelled cosine similarity with their documented range/direction and model identity; the existing `vector_score` compatibility field remains, while the explanation names the metric. Keyword lanes expose rank but no invented score. When RRF runs it exposes `k`, lane weight, `weight / (k + rank)`, and the sum before later multipliers. Single-lane and filter-only modes omit fusion fields and instead expose the real deterministic lane or filtered-most-recent sort tuple and tie-break values. Reranking identifies its model/backend and exposes the raw/adjusted direction. Reranking, boosts, and deterministic tie-breaks expose the exact before/after chain so a caller can reproduce the returned order within documented rounding. No value is called confidence or relevance unless it is actually that metric.

### Tooling can create and mutate units without string surgery

The product surface gains `observe_memory`, a focused compiled-memory operation:

- `add` adds a compact observation or rich unit to an existing writable compiled page;
- `update` replaces one addressed unit;
- `remove` removes one addressed unit;
- `validate` parses and returns the proposed result without writing.

The operation accepts parent path/reference, category, content, optional kind/tags/context, expected parent hash, and unit reference/fingerprint where applicable. Compact is the default form. Compact observations cannot carry typed unit relations; passing `relations` without an explicit non-observation governed `kind` is rejected with remediation to choose rich form or author a canonical note-level relation. A non-observation governed kind selects the rich form and may carry existing rich metadata/relations. Sources, Evidence, read-only/excluded trees, and paths outside the governed KB remain immutable through this operation.

`read_memory` can select a unit reference, and `remember`/`replace_memory`/`edit_memory` return semantic-unit feedback. Creation writers support a two-step logically atomic review protocol: a `validate_only=true` call preassigns a candidate page UUID and returns `draft_id`, a content-bound `draft_hash`, a bounded opaque `draft_token` that freezes any server-derived render date/destination and ordered project auto-registration intent, and relation candidates/findings; a commit with identical content echoes the token and may supply `draft_id`, `relation_disposition="reviewed_none"`, `relation_review_hash`, and a non-empty `relation_review_reason`. The writer revalidates the hash, token, and unused identity, persists a bounded creation receipt for every full active-compiled commit, prepares deterministic auxiliary writes first, and replaces the primary page last as the logical commit marker. Reviewed-none/bootstrap receipts also carry disposition; qualifying-relation receipts are recovery state only. Therefore a visible primary page never lacks its required review state. Abrupt process death may leave page-less prepared state; the exact unchanged draft and identical ordered auxiliary target/byte digest may resume it, while any mismatch remains reserved for explicit audit/cleanup. Auxiliary reconstruction accepts only the exact pre-write or already-applied expected state and binds replacements to exact content guards; unrelated drift requires fresh validation. An exact pre-receipt qualifying primary remains an idempotent already-committed result after upgrade, but a page-less no-receipt attempt receives no retroactive prepared-recovery claim. This is normal-return/exception atomicity plus crash-safe visibility and deterministic recovery, not a claim of impossible cross-file all-or-none power-loss atomicity. A qualifying typed relation or the first-page bootstrap needs no reviewed-none decision from the caller. All parameters and response schemas flow through the single command registry to MCP, REST, CLI, OpenAPI, and generated capability docs.

Atomic file batches use descriptor-owned stages in a random private workspace
under each target parent. Existing-file rollback state is captured in memory as
exact bytes plus the metadata the platform can safely read and restore through
descriptors: mode, nanosecond timestamps, and extended attributes where those
descriptor APIs exist. This removes named backup files and their cleanup race.
Before each replacement the writer rechecks target guards, workspace identity,
stage identity/content, already-installed finals, and directory censuses. On a
handled failure it restores existing files from fresh descriptor-owned stages
and removes a newly created final only while the supported cooperative-
concurrency assumptions still hold. Detected or ambiguous namespace drift
fails closed, retains private residue when necessary, and reports bounded
reconcile guidance rather than moving or deleting a changed path.

Stale-residue classification validates children, rechecks workspace identity
and metadata, and then ends with a fresh bounded child census as its final
namespace observation. It fails closed on unsafe state or drift detected by
those observations; directory timestamps are not treated as a replacement for
the final census. This is not a frozen directory snapshot: a same-owner
mutation after the last check relevant to a property and before the
classification result is consumed remains the same narrow post-verification
portability exclusion as final-instruction substitution.

Private workspaces, descriptor-relative traversal, and exact guards preserve
ordinary caught-failure and cooperating-writer rollback guarantees, but they
are not a same-principal security boundary. Accidental concurrent edits
observed before a destructive syscall fail closed. The excluded threat is an
uncooperative process running as the vault owner that deliberately changes a
relevant pathname after its last applicable check and before that observation
is consumed—whether by a kernel namespace instruction or a residue
classification result. A cooperative lock may serialize Exomem writers, but
it cannot provide the missing conditional identity precondition for every
pathname component or a portable frozen directory census, nor can it defend
against that uncooperative same-principal actor. The batch also makes no cross-
file power-loss atomicity claim.

Existing-page lifecycle review separates reusable review truth from transition recovery. A reviewed-none decision is an immutable, portable artifact keyed only by a unique stable page UUID and the resulting review-content fingerprint; it never binds the operation, prior bytes, transition token, or auxiliary plan. One guarded replaceable prepared-transition slot per UUID binds those transition-specific details and is crash machinery, not review truth. The current page is reviewed-none only when its unique UUID and exact current fingerprint match an immutable decision. A pending transition never blesses the still-current before state; exact retry may resume it, and a later transition back to a previously reviewed fingerprint reuses the immutable decision while creating a distinct prepared transition. Legacy path identities never receive lifecycle decisions: pages that need reviewed-none must first acquire a stable ID through the explicit backfill workflow.

Lifecycle artifacts reserve their UUID even after trash/delete. Recovery may replace a prior committed prepared slot only when the trash sidecar, original path, stable UUID, and trashed bytes exactly prove the committed after state and no live page owns that UUID. Reconcile preserves this `trashed_committed` state; it may remove only a neither-side prepared slot with no exact trash proof. This keeps exact recovery possible without allowing a later creation to steal deleted identity history.

### One semantic write contract governs every lifecycle path

`semantic_contract.evaluate()` is a pure evaluator over parsed before/after state, operation, page lifecycle, registries, saved contracts, and current review state. It returns structured errors, warnings, semantic-unit counts/findings, category findings, relation disposition, and proposed review actions. In-process writers call it in `precommit` mode and block on applicable errors; watcher/reconcile call the same evaluator in `posthoc` mode, preserve external Markdown, and surface drift. Post-commit hooks consume the same parsed state for sidecar updates.

For active governed compiled pages created after this capability is enabled, the relation disposition is satisfied by exactly one of:

1. at least one qualifying typed relation, outbound or inbound;
2. an explicit reviewed-none decision bound to the page identity and current content fingerprint; or
3. an automatic no-candidates bootstrap disposition only when the governed corpus is genuinely empty.

The qualifying predicate is exact: the edge is authored or explicitly reviewer-accepted; its target resolves unambiguously to an eligible governed page at evaluation time; its canonical registry entry is active and scope-valid; its registry family is not `link`, `citation`, `derivation`, `evidence`, `mention`, `observation`, or `provenance`; and either (a) its origin is `markdown_relation`, `semantic_relation`, or `semantic_block`, or (b) its origin is `frontmatter` and its registered family is exactly `supersession`. New authoring uses canonical `## Relations` bullets. An empty section is valid only with a current non-edge disposition. `- (none yet)` and malformed relation bullets are invalid. Inline wikilinks, unresolved/ambiguous forward targets, and generic `links_to` do not satisfy the typed relation disposition. Sources/Evidence provenance remains separately measured and does not masquerade as a semantic connection. The first-page bootstrap disposition becomes stale when the governed corpus gains another eligible page, placing that first page into ordinary relation review rather than granting a permanent exception.

Existing pages are grandfathered into activation/review rather than blocked en masse. A finding has stable identity `(code, governed_element_identity, resolved_rule)`. An in-process edit may preserve pre-existing error debt only when the after-error key set is a subset of the before-error key set and no current accepted disposition is invalidated; any new error key blocks. Replacements are new compiled conclusions and must satisfy the current contract. Direct-editor changes cannot be blocked; watcher/reconcile preserve the file, update parseable indexes, and surface violations in audit/review.

Applicability is explicit:

| Lifecycle path | Contract behavior |
|---|---|
| Active governed compiled create or replacement | Full precommit syntax, saved-schema, and relation-disposition enforcement; reviewed-none uses the draft protocol. Replacements must produce active successors. |
| Inactive governed compiled create | Structural and saved-schema precommit only; it does not join the active relation corpus or receive reviewed-none/bootstrap state. Activation later re-enters the full current contract. `draft`, `planned`, `dropped`, and `archived` are inactive; `planned` is the production-log outline phase. |
| Edit or `observe_memory` on a governed compiled page | Precommit evaluation; grandfathered errors use the before/after set rule. |
| Tier-2 create/overwrite/append under compiled-memory paths | Active compiled output receives the same full precommit contract; inactive compiled output receives structural/schema precommit; other governed-KB documents receive structural/safety checks only. |
| Adoption compile | Full precommit contract on the newly compiled output. |
| Move | Build the final corpus and reevaluate the deterministic dependency-affected closure, including unchanged pages whose relation resolution or disposition changes; always refresh path identity/index state. |
| Delete or trash | Do not content-validate the departing page; apply existing inbound-reference/lifecycle guards and clean all derived state. |
| Sources and Evidence | Never receive mutation-time semantic enforcement; read-only parse/index may expose units as raw-parent observations with provenance, not compiled conclusions. |
| Watcher and reconcile | Posthoc, nonblocking evaluation; preserve files, index valid units, and report drift. |

### Saved schemas can govern ordinary writes deliberately

Memory contracts extend from fields/blocks/relations to semantic-unit kinds and categories. A saved contract has `validation: off|warn|strict`, defaulting to `warn`. Resolution considers every project key attached to the page. For each governed rule, the highest matching specificity wins: exact project+page type, then project, then page type, then global. Equal-specificity identical rules collapse, compatible set constraints apply conjunctively, and incompatible scalar values or an empty allowed-set intersection produce a named contract conflict before an in-process write. Validation mode follows the same specificity rule; unequal modes at equal highest specificity conflict rather than silently selecting one.

`warn` permits the write and returns findings. `strict` blocks in-process create/edit/replace/observe operations before filesystem mutation. It cannot block out-of-band edits; watcher/reconcile report strict drift and keep user Markdown intact. This is stronger and more honest than claiming sync enforcement without proving the actual write call graph.

### Layered full local-core benchmark, not source-only inference

The existing direct graph benchmark becomes the single comparison harness with a versioned neutral manifest and native renderers. The Basic Memory adapter runs a pinned sibling checkout in a benchmark-managed virtual environment against a temporary project/home/config/database, performs full indexing, and talks through one persistent public MCP session. Exomem uses the same public-session rule. Mutation cases operate only on disposable corpora. The harness records revisions, dependency locks, configuration, corpus/manifest hashes, raw request/response envelopes, latency, response bytes, and before/after filesystem/database evidence.

The manifest starts with a full local knowledge-engine capability inventory reconciled against Exomem's generated command registry and each pinned contender's runtime MCP tool list/public CLI inventory. Every supported in-scope capability must map to an executed public-path runtime probe backed by a representative deterministic fixture. Only a verified unsupported result or a justified boundary exclusion may replace execution; a fixture alone never earns coverage. A newly discovered public operation makes inventory validation fail until classified. Hosting, accounts, billing, teams, cloud sync, deployment operations, and graphical interfaces are excluded. Agent-facing shared behavior is exercised over persistent MCP; a product-native CLI may cover a genuinely CLI-only local maintenance operation but cannot substitute for a missing MCP capability and is labelled by surface. The benchmark is layered so one missing optional dependency cannot hide shared-core regressions:

1. **Shared authoring and retrieval:** create/read/update notes and atomic observations/relations; permalink/title/exact lookup; rare-token and phrase/full-text cases; semantic paraphrase without lexical overlap; hybrid adversarial distractors; type/project/tag/status/date/nested numeric/category/kind filters; combined text-plus-filter and filter-only queries; score/explanation truth; one-to-three-hop typed/directional graph traversal; and bounded context assembly.
2. **Schema and lifecycle reliability:** infer/diff/validate/save schema behavior; public writes; direct filesystem edits; watcher/reconcile/full-reindex; moves, deletes, recovery, and stale-row removal; current/superseded history; and content-preserving failure behavior.
3. **Exomem local-core extensions:** durable references, Sources/Evidence and returned provenance, governed typed relations and semantic blocks, review/audit/adoption/reconcile, context packs, dataset-card/query behavior, and representative deterministic PDF/image/audio/video ingestion, search, and read behavior where the local extras are installed. Basic Memory receives `unsupported`, never a synthetic emulation, for capabilities outside its public core.

Each query family includes isolated lane probes and public hybrid probes. Score-truth cases verify that returned BM25, cosine, fusion, graph, boost, and rerank labels agree with the isolated lane membership/order and that one field never changes meaning silently between modes. Retrieval quality cases record exact expected sets/order constraints rather than judging from attractive prose. Model-backed cases pin resolved model revisions and artifact hashes for embeddings, rerankers, CLIP, ASR, and other learned components plus backend, device, dtype/quantization, runtime versions, deterministic seeds where supported, and predeclared numeric/order tolerances. Performance runs record host/OS/CPU/RAM, compute/model/backend configuration, dependency and cache state, warm-up count, repeated counterbalanced samples, timeouts, median and p95 latency, index duration, response bytes, and bounded-context size under predeclared paired non-inferiority bands. They run only on a quiesced machine and never compensate for functional errors through a weighted aggregate.

The report emits independent `shared_core`, `lifecycle_integrity`, `explanation_truth`, `performance_envelope`, and `exomem_extensions` gates. Unsupported behavior on a contender-neutral shared-core case counts as not passed, not as an exemption. A recorded local-core advantage may be claimed only for the pinned revisions/corpus when preflight proves both environments valid and every required probe completes as pass, behavioral fail, or verified unsupported; every required Exomem shared-core case and outcome passes; Exomem passes every individual case that Basic Memory passes; all required Exomem fixture invariants and paired performance/no-regression thresholds pass; every advertised in-scope Exomem extension designated required by the full profile passes in the pinned extras environment; and at least one such extension is publicly absent from Basic Memory. A case both contenders fail still blocks the full claim, and failures are never hidden inside a coarse dimension aggregate. A valid public operation returning an error is a behavioral result under the predeclared policy, while harness/setup/adapter failure invalidates the claim rather than counting as a contender loss. A lean run with missing media/model extras may still report shared-core results but cannot produce the full local-core-advantage claim. Any unsupported behavior, execution failure, configuration difference, or dependency omission remains visible. The result is never generalized to hosting or overall product superiority, and neither contender is pointed at a live user vault.

## Data Flow

1. A writer, watcher, reconcile, or index rebuild reads Markdown once.
2. The semantic-unit parser returns units, note relations, and diagnostics with source spans.
3. The semantic contract resolves registries, schemas, page lifecycle, and review dispositions.
4. In-process writers stop on contract errors or atomically commit the file batch.
5. One post-commit coordinator applies the same parsed result to lexical, embedding, and graph sidecars; stale records for that parent are replaced transactionally within each sidecar.
6. Recall normalizes shortcuts and `filters`, filters candidates before ranking, ranks page and/or unit records, and returns parent-aware citations plus optional reproducible explanation evidence.
7. Context assembly reuses selected unit records, adds bounded graph/provenance/lifecycle context, and reports truncation.
8. Every derived record carries the same `parent_generation`, parent source hash, and parser schema version. Query-time validation compares candidate records with the current on-disk parent hash and rejects absent, mismatched, or mixed-generation records; a partial sidecar update can temporarily omit fresh results but cannot surface stale identity/content as current.
9. Watcher/reconcile follows the same parse/index path; it reports but never destroys externally authored invalid Markdown and repairs incomplete generations.

## Error Handling

- Parser diagnostics include stable codes, path, line/span, raw fragment, and remediation.
- Strict contract failures occur before filesystem writes and use the shared error envelope across surfaces.
- Expected-hash and unit-fingerprint mismatches return stale-reference errors with the current parent hash; no best-effort mutation occurs.
- Sidecar update failure after a committed Markdown write marks deterministic index drift and schedules/recommends reconcile; Markdown success is not rolled back by deleting user content. Any old-generation candidates fail the current-parent hash check until repaired.
- Immediate index feedback distinguishes configured/unavailable acceptance, process-local warmup deferral, durable deferred-index work, proved completion, and observed degradation. Legacy sidecars that swallow failures are reported as accepted-unverified rather than falsely proved successful; audit/reconcile remains authoritative for their drift. Each required upsert or delete fan-out is invoked exactly once, and no second call is manufactured for feedback.
- Optional embedding import/model failures are reported as degraded unit retrieval and fall back to lexical/category filtering.
- Filter validation fails before retrieval with the exact field path/operator and expected value shape; unsupported backend filtering never silently broadens the query.
- Explanation reports missing/degraded lanes and unavailable measurements explicitly; it never substitutes zero or overloads one score field across retrieval modes.
- Unregistered categories remain valid unless an explicitly resolved contract forbids them. Unregistered relations remain preserved but semantically inert under the existing relation-registry rules and cannot satisfy the relation disposition.
- Category-registry or contract conflicts fail closed for in-process strict writes and surface actionable audit findings for existing/out-of-band content.

## Risks / Trade-offs

- [Risk] Parsing bracketed bullets anywhere can classify unrelated prose as observations. → Exclude task boxes/fences, use a strict grammar, preserve spans, make canonical authoring section-scoped, and benchmark false positives on the existing vault before changing defaults.
- [Risk] Unit-level retrieval floods results from a single page. → Keep page-level default, require explicit/implicit unit mode through filters, cap per-parent results in mixed mode, and expose truncation/grouping.
- [Risk] A generic filter DSL becomes an unsafe query language or diverges by backend. → Restrict namespaces/operators/types/depth/clause count, compile to one typed AST, and conformance-test every backend against the same fixture matrix.
- [Risk] Raw scores look authoritative or comparable when they are not. → Label metric/backend/normalization, expose the ordering chain rather than a synthetic confidence, and test explanation truth against isolated lanes.
- [Risk] Open categories fragment into near-duplicates. → Preserve raw labels, normalize exact matching, surface frequency/alias proposals, and require review before registry changes.
- [Risk] Automatic category-to-kind inference launders semantics. → Never infer kind from an open category; only explicit rich headings/structured tool inputs assign governed kinds.
- [Risk] Multiple derived indexes drift. → Parse once, stamp every record with one parent generation/source hash, reject mismatches at query time, replace parent-owned rows transactionally per sidecar, and make reconcile authoritative.
- [Risk] Strict schemas make legacy notes uneditable. → Default to warn, grandfather existing debt, require explicit strict activation, and report resolution precedence.
- [Risk] Relation enforcement makes the first note impossible. → Permit the narrowly defined empty-corpus bootstrap disposition; require review once real candidates can exist.
- [Risk] The direct contender's documentation differs from runtime or its environment drifts. → Pin the sibling revision and lock/config hashes, score executed public behavior only, and keep unsupported/contradictory claims visible.
- [Risk] Scope grows across too many modules. → Implement in dependency-ordered slices around the parser/contract boundary, maintain additive defaults, and require focused + lean-suite gates after every slice.

## Migration Plan

1. Ship parser/model and read-only census behind no feature flag; unknown existing syntax remains Markdown. First activation writes one portable governed baseline manifest but does not rewrite existing pages.
2. Add sidecar schema versions and rebuild paths; compare unit census/false positives on fixtures and a read-only existing-vault scan.
3. Add read/context and explicit unit retrieval while preserving page-default bytes.
4. Add the typed filter compiler and backend conformance before exposing generic filters on public surfaces.
5. Propagate lane measurements and add opt-in retrieval explanations without changing default envelopes.
6. Add category/kind schema inference and proposal-only registries.
7. Add `observe_memory` and shared warn-mode writer feedback.
8. Enable strict saved-contract behavior only when a user explicitly saves/activates it.
9. Activate the new-page relation contract; grandfather existing pages into the review queue.
10. Land the layered local-core benchmark, pin/setup its isolated Basic Memory environment, and close or explicitly record every shared-core gap before making a revision-bound claim.
11. Update the generic scaffold, docs, generated surfaces, and capability snapshots.

Rollback disables unit indexing/retrieval and restores the prior sidecar schema. No Markdown rollback or migration is required because the syntax is ordinary Markdown and all new indexes are derived. Explicit compact observations remain readable text if an older Exomem version is used.

## Open Questions

None. The user selected open Basic Memory-compatible categories with stronger Exomem governance, one namespaced filter language, opt-in score explanations, and a layered full local-core comparison. Runtime uncertainties about Basic Memory behavior are benchmark questions, not design blockers.
