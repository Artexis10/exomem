# First-class semantic language

Exomem keeps Markdown readable while making small observations, richer typed
units, retrieval filters, ranking evidence, and authored graph relations
addressable through the public product commands. Markdown remains the source of
truth; lexical, vector, and graph stores are rebuildable sidecars.

## Author compact observations and rich units

A compact observation is one line:

```markdown
- [config] Embeddings are disabled on battery power. #runtime (laptop) ^battery-policy
```

Its governed `kind` is always `observation`. Its `category` is open vocabulary
and resolves through the semantic-language registry when an alias exists.
Categories describe what an observation is about; kinds select the governed
shape and contract.

A rich unit uses a governed non-observation heading and may author typed unit
relations:

```markdown
## Decision
- id: local-index-policy
- category: architecture
- relations: supported_by: [[Evidence#latency-run]]

Keep the primary index local and rebuildable.
```

Use `observe_memory(operation="add"|"update"|"remove"|"validate")` for one
unit. Update and remove operations must carry both the current parent
`content_hash` and the unit fingerprint. Compact observations cannot author
typed unit relations: use a rich unit or a canonical note-level relation under
`## Relations`.

## Retrieve pages, units, or both

`ask_memory` and `find` accept `result_level="page"`, `"unit"`, or `"mixed"`.
`auto` preserves the normal page-first product behavior unless unit predicates
require a unit-capable result.

```json
{
  "query": "battery policy",
  "categories": ["config"],
  "kinds": ["observation"],
  "result_level": "mixed"
}
```

The `categories` and `kinds` lists are shortcuts. The general `filters` object
uses a bounded typed expression over reserved `page.*` fields, RFC-6901
frontmatter paths, and the closed `unit.*` field set. Lists are OR within an
axis; category/kind/text/filter axes combine with AND. Invalid or over-limit
plans fail before candidate retrieval and never silently widen.

```json
{
  "query": "",
  "filters": {
    "$and": [
      {"page.type": {"$eq": "insight"}},
      {"unit.category": {"$in": ["config", "rule"]}},
      {"page.frontmatter./status": {"$ne": "superseded"}}
    ]
  },
  "result_level": "unit"
}
```

An empty query plus filters is a filter-only lookup. It uses the documented
filtered-most-recent sort tuple; it does not fabricate a text score.

## Explain ranking without inventing confidence

Set `explain=true` only when ranking interpretation is useful. The response
adds a versioned, bounded `retrieval_profile` and per-hit
`ranking_explanation`; the default response remains unchanged when explanation
is off.

The profile reports requested/effective modes and result levels, normalized
filters, participating and unavailable lanes, backend/model/metric metadata,
fusion constants, rerank decisions, and final ordering policy. Per-hit evidence
contains only lanes that actually participated.

The values are deliberately separate:

- BM25 is a backend relevance measurement with the direction/range declared by
  the profile.
- cosine is vector similarity, not probability.
- RRF contribution is exact rank-fusion math and exists only when fusion ran.
- reranker raw and adjusted values are separate when reranking ran.
- final rank follows the recorded boost, rerank, and deterministic tie-break
  chain.

None of these is a confidence score. A disabled, unavailable, warming, or
nonparticipating lane is reported at profile level and is never represented as
a fabricated per-hit zero.

## Read and traverse exact units

Unit-level recall returns an exact `unit_ref`. Pass it to `read_memory` to read
the current unit with bounded parent context. Missing, stale, ambiguous, and
superseded references are explicit statuses; Exomem never substitutes a nearby
unit.

`connect_memory(operation="graph-context")` accepts an exact `unit_ref` or
registry-resolved `categories`/`kinds` filters. Compact and rich units appear as
derived graph nodes, but traversal follows authored edges only. Exomem does not
infer a typed edge from a compact observation's category or prose.

## Reviewed creation and adoption

For a validate/review/commit creation flow, call the intended writer with
`validate_only=true`, retain the returned `draft_id`, `draft_hash`, candidate,
and semantic feedback, then commit the unchanged draft through the same writer.
If a governed qualifying relation has no accepted edge, use the returned
relation review hash and an explicit reason; never invent a reviewed-none
decision from an empty Relations section.

`adopt_vault(mode="scan-only")` includes a bounded read-only semantic census:
compact/rich counts, raw/canonical/resolved categories, collisions, malformed
candidates, contract/schema debt, relation dispositions, coverage, and safe
next actions. It never rewrites originals or requires categories.
`compile-selected` may copy selected legacy text into governed Sources and then
returns a proposal only. Review it and hand it to `remember` (the `note()`
writer); normal semantic precommit remains mandatory.

Default initialization, scanning, reading, registry upgrades, and index
rebuilds do not fabricate observations, categories, relations, page IDs, or
review decisions. Existing pages are activation-snapshotted by stable ID or
portable path/hash fallback without rewriting their Markdown.
