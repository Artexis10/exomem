## Context

Exomem already derives a graph from Markdown files, semantic blocks,
frontmatter, wikilinks, and explicit typed relation bullets. The current
relation vocabulary is duplicated in `semantic_blocks.py` and
`epistemic_graph.py`; unsupported labels are ignored. That works for a compact
global ontology but loses useful observations in broad corpora and makes adding
domain precision a code release rather than a governed knowledge decision.

The intended corpus spans science, medicine, technology, health, fitness,
wellbeing, entertainment, and future domains. Those domains need precise terms
such as `medicine.contraindicates`, `science.replicates`, or
`software.regressed_by`, while cross-domain context still needs to understand
that these refine core contradiction, support, and causality families.

Markdown remains canonical. SQLite graph state remains rebuildable. Exomem may
parse, normalize, count, validate, and traverse relations deterministically, but
it does not decide whether a claim is true, infer logical closure, or silently
accept model-suggested semantics. Merged PR #182 supplies the stable-reference,
unified-context, and schema-governance surfaces this design extends.

## Goals / Non-Goals

**Goals:**

- Establish one versioned source of truth for portable core relation semantics.
- Permit precise namespaced extension relations without fragmenting
  cross-domain traversal.
- Preserve unknown observed relation labels and their provenance for review.
- Infer corpus relation usage and propose registry additions without automatic
  adoption.
- Offer deterministic traversal lenses over the same stored graph.
- Keep registry/profile changes explicit, hash-guarded, auditable, rebuildable,
  and consistent across every product surface.

**Non-Goals:**

- No server-side reasoning model, ontology-generation agent, or automatic
  relation acceptance.
- No automatic transitive closure, causal inference, contradiction judgment,
  or truth/confidence score.
- No canonical graph database and no required rewrite of existing Markdown.
- No per-project redefinition of a relation's meaning.
- No full ontology editor, OWL/RDF compatibility layer, or public taxonomy
  marketplace in this change.
- No change to default `find` ordering or retrieval ranking.

## Decisions

### One immutable core registry replaces duplicated enums

Add a small `relation_registry` module backed by one packaged, versioned core
definition. Semantic parsing, graph indexing, validation, suggestions, context,
and tests consume this registry rather than maintaining separate sets.

Each core definition carries a canonical key, description, family, directed or
symmetric behavior, optional inverse key, permitted deterministic origins, and
deprecation metadata. The first registry version preserves every currently
accepted relation and its behavior.

A packaged registry was selected over copying the core definitions into every
vault. The core must mean the same thing across installations and must not be
silently overridden by vault configuration. A Python-only enum was rejected
because the registry needs structured metadata and stable serialization for
API responses and tests.

### Extensions are namespaced refinements of a core parent

Vault-owned extensions live in
`Knowledge Base/_Schema/relation-registry.yaml`. The generic scaffold starts
with a versioned empty extension list and examples only in comments/guidance.
An extension definition contains:

- a namespaced canonical key such as `medicine.contraindicates`;
- exactly one core parent such as `contradicts`;
- a human-readable description;
- optional aliases and inverse display key;
- allowed source and target node/block kinds;
- an applicability scope over project keys and/or page types;
- `active` or `deprecated` status and optional `replaced_by`;
- allowed observation origins, defaulting to explicit semantic relations.

Keys use a strict lowercase `<namespace>.<name>` form. Core keys, extension
keys, and aliases are globally unique within a vault. Extensions cannot replace
or shadow core definitions. Applicability scope controls where an extension is
valid; it never changes the extension's meaning. A use outside scope remains
observable but receives a scope-violation finding.

Parent mapping was selected over unrelated custom labels because it gives a
portable graph view: an epistemic traversal that includes `contradicts` can also
include `medicine.contraindicates`. A fully fixed global enum was rejected as
too shallow for the corpus. Arbitrary free-form registered labels were rejected
because synonyms would fragment the graph and make cross-domain queries
unreliable.

### Edges retain raw observation and registry resolution separately

The derived graph edge schema records:

- `raw_relation`, exactly as observed in Markdown;
- `relation_type`, the canonical core or extension key when resolved;
- `parent_relation`, populated for extensions;
- `registry_status`: `core`, `extension`, `alias`, `unregistered`,
  `deprecated`, or `scope_violation`;
- `registry_version` and extension-registry content hash;
- existing origin, source path, source anchor, source hash, and metadata.

Alias resolution changes only derived canonicalization; Markdown is not
rewritten automatically. Deprecated relations remain resolvable and visible.
Rebuilds are deterministic for a given Markdown corpus plus registry version and
extension hash.

Keeping the raw observation separate avoids laundering an author-provided word
into a stronger canonical claim. It also makes migrations and alias changes
reviewable.

### Unknown relation observations are preserved but semantically inert

The parser recognizes syntactically valid typed-relation observations even when
their label is not registered. The graph stores the observed edge with
`registry_status="unregistered"`, its raw label, target resolution, and source
provenance. It does not assign a core parent, inverse, symmetry, or epistemic
meaning.

Normal traversal profiles exclude unregistered edges. Unified context reports
their count and examples as warnings; the unrestricted diagnostic profile may
include them explicitly. Audit reports them, and corpus inference can propose
registry skeletons.

Preservation was selected over the current ignore behavior because dropped
observations cannot be governed later. Treating unknown labels as generic
`links_to` was rejected because that erases the author's typed intent and makes
the graph appear more certain than it is.

### Corpus inference proposes registry skeletons, never semantics

Schema governance gains relation-registry as a subject alongside corpus
contracts. Deterministic inference reports registered, deprecated,
out-of-scope, and unregistered relation frequencies for a project/page-type
scope, with bounded example paths and anchors. An unregistered label produces a
proposal skeleton whose parent and description remain unset unless a unique
declared alias resolves them.

Saving is explicit and requires a complete caller-reviewed proposal. Existing
registry overwrite requires the current content hash. The write refuses missing
parents, alias collisions, core shadowing, invalid scope keys, inverse cycles,
and deprecation targets that do not resolve.

An optional model may suggest a description or parent only when explicitly
requested. This is pure-substrate-safe because the suggestion is response-only,
default-off, clearly attributed, and soft-fails when unavailable. It can never
populate the saved proposal without the caller sending the reviewed definition
back through the guarded save path.

### Traversal profiles are deterministic query policy, not graph truth

Ship these built-in profiles:

- `epistemic`: support, contradiction, refinement, duplication,
  supersession, questions, and answers;
- `provenance`: derivation, evidence, citation, and observation;
- `causal`: causes, caused-by, dependency, mitigation, blocking, and
  resolution;
- `decision`: evidence/derivation, dependency, implementation, use,
  mitigation, and resolution;
- `all`: every registered relation, preserving current broad graph-context
  behavior.

Custom profiles live in
`Knowledge Base/_Schema/traversal-profiles.yaml`. A custom profile extends one
built-in profile and may add/remove core families or exact relation keys, set
edge direction, choose whether parent matches include extensions, provide a
deterministic relation-priority order, and lower default depth/node/edge caps.
It cannot exceed server hard caps or include unregistered relations except in an
explicit diagnostic mode.

`connect_memory(operation="context")` accepts `traversal_profile`; omission
uses `all` for compatibility. Explicit `relation_types` intersect the profile
rather than expanding it. Runtime depth and caps may further restrict the
profile. The response names the resolved profile, registry version/hash,
included relation families, unknown counts, and every applied truncation.

Profiles do not mutate Markdown, accept relations, change default `find`
ranking, or express epistemic confidence. Deterministic priority exists only to
choose which edges fit inside a context cap.

### Schema governance is extended instead of adding another top-level tool

After PR #182, `schema_memory` remains the advanced governance surface. Add a
backward-compatible `subject` parameter with default `contract` and values
`relations` and `traversal-profiles`. Existing infer/validate/diff, `save`,
`expected_hash`, scope, strict-exit, and proposal-first behavior are reused.
A structured `proposal` parameter lets MCP/REST callers return a reviewed
inference proposal for guarded persistence; CLI accepts the same JSON object.

`connect_memory` remains the read surface. This avoids a new graph-admin tool
while keeping registry and profile semantics identical across MCP, REST, CLI,
OpenAPI, and generated docs.

### Registry/profile changes invalidate graph state by content hash

The graph sidecar metadata stores core registry version, extension-registry
hash, and traversal-profile hash. Only the first two affect indexed edge
resolution; a mismatch makes graph data stale and triggers rebuild through the
existing writer/watcher/reconcile paths. Profile-only changes invalidate cached
context plans but do not require reindexing edges.

Audit detects missing, malformed, stale, conflicting, or out-of-scope registry
state. Reconcile rebuilds derived graph state without modifying Markdown or
registry files.

## Risks / Trade-offs

- [Risk] Domain extensions create synonyms and ontology sprawl. -> Require a
  core parent, namespace, description, global alias uniqueness, corpus evidence,
  and explicit adoption; surface near-duplicate names during inference.
- [Risk] Parent mappings imply more semantics than authors intended. -> Preserve
  raw labels, show parent expansion in context metadata, and never rewrite or
  infer logical conclusions from ancestry.
- [Risk] Unknown-edge preservation increases graph size and noise. -> Bound
  examples, exclude unknown edges from normal profiles, and keep their payload
  compact and derived.
- [Risk] A custom profile hides important opposing evidence. -> Always report
  the active profile and excluded/unknown counts; provide `all` and
  `epistemic` comparison in diagnostics; never call a profile exhaustive.
- [Risk] Registry edits make sidecars stale. -> Hash registry inputs into graph
  metadata, audit drift, and rebuild deterministically through reconcile.
- [Risk] Scope rules become another closed taxonomy. -> Projects/page types
  remain open sets governed by existing schema rules; scope is optional and
  unknown content stays allowed by default.
- [Risk] Optional model parent suggestions violate the substrate boundary. ->
  Default off, response-only, explicitly attributed, soft-failing, and
  impossible to persist without a reviewed proposal echoed through save.
- [Trade-off] V1 deliberately excludes transitivity and rule inference. This
  sacrifices automatic deductions to preserve epistemic honesty and avoid
  domain-invalid closure.

## Migration Plan

1. Start from the merged PR #182 stable-reference, unified-context, and
   schema-governance foundation.
2. Introduce the core registry with parity tests proving every existing
   relation still parses and indexes identically.
3. Add the empty generic extension/profile scaffold and validation without
   changing graph behavior.
4. Bump the graph sidecar schema, preserve raw/unknown observations, and rebuild
   derived state through reconcile.
5. Add relation inference and guarded persistence to `schema_memory`.
6. Add built-in/custom profile resolution to unified context, then update
   generated schemas, docs, and product E2E.
7. Run dry-run inference on real broad corpora before registering any extension;
   ship no personal/domain-specific extensions in the generic scaffold.

Rollback ignores the optional extension/profile files, restores the prior core
registry version, and rebuilds the derived graph. Markdown observations remain
unchanged, so no content migration or destructive rollback is required.

## Open Questions

None for implementation start. The core-vs-extension boundary, namespace form,
unknown-edge behavior, profile semantics, write guard, and PR #182 dependency
are selected here; proposed real-world extensions remain corpus decisions made
after inference and review.
