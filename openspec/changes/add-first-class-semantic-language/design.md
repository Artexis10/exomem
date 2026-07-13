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
- Keep categories open and ergonomic while making their raw/canonical identity, aliases, deprecation, scope, and contract use reviewable.
- Apply one semantic contract to all in-process writers and surface out-of-band violations without destroying user edits.
- Prove common no-regression and scoped Exomem semantic-governance differentiation through isolated end-to-end fixtures against the sibling Basic Memory checkout.
- Preserve empty-vault onboarding and existing-vault adoption without bulk rewriting Markdown.

**Non-Goals:**

- Hosting, sync, collaboration, billing, web editing, or other cloud-product breadth.
- A server-side reasoning or ontology-inference model.
- Automatically deciding whether a statement is true, authoritative, contradictory, or semantically related.
- Requiring every paragraph or bullet to become a semantic unit.
- Rewriting existing notes into compact observations or adding visible IDs in bulk.
- Making every category a global governed enum or silently mapping domain labels such as `term`, `rule`, or `config` onto epistemic kinds.
- Replacing page-level retrieval, semantic blocks, canonical note relations, or Markdown with a database-owned object model.

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
- `kinds`: exact governed kind filters, OR within the list.

With no unit filters, `auto` resolves to `page` and preserves the existing result bytes and ordering. Passing `categories` or `kinds` makes `auto` resolve to `unit`. Values are ORed within each filter list, while text, category, and kind axes are ANDed when supplied together. `page` with unit filters returns parent pages whose units match and carries a bounded `matched_units` annotation. `unit` returns independently ranked semantic-unit hits. `mixed` fuses page and unit candidates, caps repeated units per parent, and preserves explicit result identity.

Unit hits carry `result_type="semantic_unit"`, unit/category/kind fields, parent path/reference/title/type/status, anchor/span, excerpt/content, and ranking signals. Category-only search works with an empty text query. Exact filters apply before ranking, so text mentioning the word `decision` cannot satisfy `categories=["decision"]` unless the parsed unit is actually categorized `decision`.

Lexical unit records live in the existing lexical sidecar with a record discriminator. Optional unit embeddings live in the existing embedding sidecar with a unit key and parent path. The epistemic graph adds compact unit nodes and `derived_from` edges; authored rich-block relations retain their current behavior and existing rich-block graph node keys. One normalized rich unit produces one graph node/edge identity. The legacy `semantic_blocks` context field becomes a bounded compatibility projection from `semantic_units`; it does not trigger a second parse, row set, graph node, or duplicate result. A schema-version bump triggers rebuild rather than Markdown migration.

### Tooling can create and mutate units without string surgery

The product surface gains `observe_memory`, a focused compiled-memory operation:

- `add` adds a compact observation or rich unit to an existing writable compiled page;
- `update` replaces one addressed unit;
- `remove` removes one addressed unit;
- `validate` parses and returns the proposed result without writing.

The operation accepts parent path/reference, category, content, optional kind/tags/context, expected parent hash, and unit reference/fingerprint where applicable. Compact is the default form. Compact observations cannot carry typed unit relations; passing `relations` without an explicit non-observation governed `kind` is rejected with remediation to choose rich form or author a canonical note-level relation. A non-observation governed kind selects the rich form and may carry existing rich metadata/relations. Sources, Evidence, read-only/excluded trees, and paths outside the governed KB remain immutable through this operation.

`read_memory` can select a unit reference, and `remember`/`replace_memory`/`edit_memory` return semantic-unit feedback. Creation writers support a two-step atomic review protocol: a `validate_only=true` call preassigns a candidate page UUID and returns `draft_id`, a content-bound `draft_hash`, and relation candidates/findings; a commit with identical content may supply `draft_id`, `relation_disposition="reviewed_none"`, `relation_review_hash`, and a non-empty `relation_review_reason`. The writer revalidates the hash and unused identity, then atomically commits the page identity/content plus portable fingerprint-bound review state. A qualifying typed relation or the first-page bootstrap needs no reviewed-none token. All parameters and response schemas flow through the single command registry to MCP, REST, CLI, OpenAPI, and generated capability docs.

### One semantic write contract governs every lifecycle path

`semantic_contract.evaluate()` is a pure evaluator over parsed before/after state, operation, page lifecycle, registries, saved contracts, and current review state. It returns structured errors, warnings, semantic-unit counts/findings, category findings, relation disposition, and proposed review actions. In-process writers call it in `precommit` mode and block on applicable errors; watcher/reconcile call the same evaluator in `posthoc` mode, preserve external Markdown, and surface drift. Post-commit hooks consume the same parsed state for sidecar updates.

For governed compiled pages created after this capability is enabled, the relation disposition is satisfied by exactly one of:

1. at least one qualifying typed relation, outbound or inbound;
2. an explicit reviewed-none decision bound to the page identity and current content fingerprint; or
3. an automatic no-candidates bootstrap disposition only when the governed corpus is genuinely empty.

The qualifying predicate is exact: the edge is authored or explicitly reviewer-accepted; its target resolves unambiguously to an eligible governed page at evaluation time; its canonical registry entry is active and scope-valid; its registry family is not `link`, `citation`, `derivation`, `evidence`, `mention`, `observation`, or `provenance`; and either (a) its origin is `markdown_relation`, `semantic_relation`, or `semantic_block`, or (b) its origin is `frontmatter` and its registered family is exactly `supersession`. New authoring uses canonical `## Relations` bullets. An empty section is valid only with a current non-edge disposition. `- (none yet)` and malformed relation bullets are invalid. Inline wikilinks, unresolved/ambiguous forward targets, and generic `links_to` do not satisfy the typed relation disposition. Sources/Evidence provenance remains separately measured and does not masquerade as a semantic connection. The first-page bootstrap disposition becomes stale when the governed corpus gains another eligible page, placing that first page into ordinary relation review rather than granting a permanent exception.

Existing pages are grandfathered into activation/review rather than blocked en masse. A finding has stable identity `(code, governed_element_identity, resolved_rule)`. An in-process edit may preserve pre-existing error debt only when the after-error key set is a subset of the before-error key set and no current accepted disposition is invalidated; any new error key blocks. Replacements are new compiled conclusions and must satisfy the current contract. Direct-editor changes cannot be blocked; watcher/reconcile preserve the file, update parseable indexes, and surface violations in audit/review.

Applicability is explicit:

| Lifecycle path | Contract behavior |
|---|---|
| Governed compiled create or replacement | Full precommit syntax, saved-schema, and relation-disposition enforcement; reviewed-none uses the draft protocol. |
| Edit or `observe_memory` on a governed compiled page | Precommit evaluation; grandfathered errors use the before/after set rule. |
| Tier-2 create/overwrite/append under compiled-memory paths | Same full precommit contract; other governed-KB documents receive structural/safety checks only. |
| Adoption compile | Full precommit contract on the newly compiled output. |
| Move | Reevaluate only rules affected by path, project, page-type, or scope change; always refresh path identity/index state. |
| Delete or trash | Do not content-validate the departing page; apply existing inbound-reference/lifecycle guards and clean all derived state. |
| Sources and Evidence | Never receive mutation-time semantic enforcement; read-only parse/index may expose units as raw-parent observations with provenance, not compiled conclusions. |
| Watcher and reconcile | Posthoc, nonblocking evaluation; preserve files, index valid units, and report drift. |

### Saved schemas can govern ordinary writes deliberately

Memory contracts extend from fields/blocks/relations to semantic-unit kinds and categories. A saved contract has `validation: off|warn|strict`, defaulting to `warn`. Resolution considers every project key attached to the page. For each governed rule, the highest matching specificity wins: exact project+page type, then project, then page type, then global. Equal-specificity identical rules collapse, compatible set constraints apply conjunctively, and incompatible scalar values or an empty allowed-set intersection produce a named contract conflict before an in-process write. Validation mode follows the same specificity rule; unequal modes at equal highest specificity conflict rather than silently selecting one.

`warn` permits the write and returns findings. `strict` blocks in-process create/edit/replace/observe operations before filesystem mutation. It cannot block out-of-band edits; watcher/reconcile report strict drift and keep user Markdown intact. This is stronger and more honest than claiming sync enforcement without proving the actual write call graph.

### Isolated scoped outcome benchmark, not source-only inference

The existing direct graph benchmark will be generalized with a neutral semantic-language manifest and two native renderers. The Basic Memory adapter runs a sibling checkout against a temporary project/home/config/database, performs full indexing, and talks through one persistent public MCP session. Mutation cases use only the throwaway corpus. The harness records revisions, configuration, corpus hashes, raw envelopes, latency, and mutation diffs.

The manifest predeclares corpus, contender revisions, dimensions, pass criteria, latency/response-size thresholds, normalization, and unsupported/error handling before a run. Contender-neutral user outcomes are exact knowledge-unit retrieval, source-location citation, current/history distinction, safe schema enforcement, external-edit repair without content loss, typed relation direction fidelity, bounded context, and complete mutation cleanup. Supporting measures cover open category parsing, same-content/different-category identity, category-only/text/hybrid recall, edits, moves, deletes, and multi-hop traversal.

The report keeps every dimension independent and may claim a scoped semantic-governance advantage for the recorded revisions only when Exomem passes every required user outcome, stays within every predeclared no-regression threshold, and demonstrates strictly more of the governed outcomes. Unsupported behavior is reported, never awarded. It never claims overall product superiority, and the harness does not point Basic Memory at a live user vault.

## Data Flow

1. A writer, watcher, reconcile, or index rebuild reads Markdown once.
2. The semantic-unit parser returns units, note relations, and diagnostics with source spans.
3. The semantic contract resolves registries, schemas, page lifecycle, and review dispositions.
4. In-process writers stop on contract errors or atomically commit the file batch.
5. One post-commit coordinator applies the same parsed result to lexical, embedding, and graph sidecars; stale records for that parent are replaced transactionally within each sidecar.
6. Recall filters/ranks page and/or unit records and returns parent-aware citations.
7. Context assembly reuses selected unit records, adds bounded graph/provenance/lifecycle context, and reports truncation.
8. Every derived record carries the same `parent_generation`, parent source hash, and parser schema version. Query-time validation compares candidate records with the current on-disk parent hash and rejects absent, mismatched, or mixed-generation records; a partial sidecar update can temporarily omit fresh results but cannot surface stale identity/content as current.
9. Watcher/reconcile follows the same parse/index path; it reports but never destroys externally authored invalid Markdown and repairs incomplete generations.

## Error Handling

- Parser diagnostics include stable codes, path, line/span, raw fragment, and remediation.
- Strict contract failures occur before filesystem writes and use the shared error envelope across surfaces.
- Expected-hash and unit-fingerprint mismatches return stale-reference errors with the current parent hash; no best-effort mutation occurs.
- Sidecar update failure after a committed Markdown write marks deterministic index drift and schedules/recommends reconcile; Markdown success is not rolled back by deleting user content. Any old-generation candidates fail the current-parent hash check until repaired.
- Optional embedding import/model failures are reported as degraded unit retrieval and fall back to lexical/category filtering.
- Unregistered categories remain valid unless an explicitly resolved contract forbids them. Unregistered relations remain preserved but semantically inert under the existing relation-registry rules and cannot satisfy the relation disposition.
- Category-registry or contract conflicts fail closed for in-process strict writes and surface actionable audit findings for existing/out-of-band content.

## Risks / Trade-offs

- [Risk] Parsing bracketed bullets anywhere can classify unrelated prose as observations. → Exclude task boxes/fences, use a strict grammar, preserve spans, make canonical authoring section-scoped, and benchmark false positives on the existing vault before changing defaults.
- [Risk] Unit-level retrieval floods results from a single page. → Keep page-level default, require explicit/implicit unit mode through filters, cap per-parent results in mixed mode, and expose truncation/grouping.
- [Risk] Open categories fragment into near-duplicates. → Preserve raw labels, normalize exact matching, surface frequency/alias proposals, and require review before registry changes.
- [Risk] Automatic category-to-kind inference launders semantics. → Never infer kind from an open category; only explicit rich headings/structured tool inputs assign governed kinds.
- [Risk] Multiple derived indexes drift. → Parse once, stamp every record with one parent generation/source hash, reject mismatches at query time, replace parent-owned rows transactionally per sidecar, and make reconcile authoritative.
- [Risk] Strict schemas make legacy notes uneditable. → Default to warn, grandfather existing debt, require explicit strict activation, and report resolution precedence.
- [Risk] Relation enforcement makes the first note impossible. → Permit the narrowly defined empty-corpus bootstrap disposition; require review once real candidates can exist.
- [Risk] The direct contender's documented strict schema behavior differs from runtime. → Score executed public behavior only and keep unsupported/contradictory claims visible in benchmark output.
- [Risk] Scope grows across too many modules. → Implement in dependency-ordered slices around the parser/contract boundary, maintain additive defaults, and require focused + lean-suite gates after every slice.

## Migration Plan

1. Ship parser/model and read-only census behind no feature flag; unknown existing syntax remains Markdown. First activation writes one portable governed baseline manifest but does not rewrite existing pages.
2. Add sidecar schema versions and rebuild paths; compare unit census/false positives on fixtures and a read-only existing-vault scan.
3. Add read/context and explicit unit retrieval while preserving page-default bytes.
4. Add category/kind schema inference and proposal-only registries.
5. Add `observe_memory` and shared warn-mode writer feedback.
6. Enable strict saved-contract behavior only when a user explicitly saves/activates it.
7. Activate the new-page relation contract; grandfather existing pages into the review queue.
8. Land the direct semantic-language benchmark and require the scoped outcome gate before claiming a recorded core semantic-governance advantage.
9. Update the generic scaffold, docs, generated surfaces, and capability snapshots.

Rollback disables unit indexing/retrieval and restores the prior sidecar schema. No Markdown rollback or migration is required because the syntax is ordinary Markdown and all new indexes are derived. Explicit compact observations remain readable text if an older Exomem version is used.

## Open Questions

None. The user selected open Basic Memory-compatible categories with stronger Exomem governance and authorized the complete core-product implementation. Runtime uncertainties about Basic Memory schema enforcement are benchmark questions, not design blockers.
