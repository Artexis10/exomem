## Why

The strongest contradiction signal in the vault is the only one review ignores. A
user who writes `- contradicts [[Other Note]]` under `## Relations` has asserted a
conflict in their own words, yet that authored edge feeds no review queue at all.
The `corpus_contradictions` queue is stance-blind by construction: it measures
embedding proximity, and cosine cannot separate "X works" from "X fails". Near
duplicate detection exists only as a write-time draft warning with no standing
queue behind it.

The result is backwards. Assert the contradiction explicitly, or write both rivals
as proper notes, and review support disappears; leave the tension implicit and only
then does a proximity band notice it. Two notes that are genuine competing
alternatives ("we keep both on purpose") have no way to say so, so the write-time
overlap warning nags on every edit to either one.

## What Changes

- Surface authored `contradicts` graph edges as asserted entries in the
  `corpus_contradictions` queue, ranked above every proximity pair and carrying the
  same fingerprint-bound triage contract as existing entries.
- Emit asserted entries without embeddings: the proximity sweep still short-circuits
  under `EXOMEM_DISABLE_EMBEDDINGS`, but an authored edge needs no sidecar.
- Label every contradiction entry with an explicit `provenance` of `asserted` or
  `proximity`, and suppress the proximity duplicate of a pair already surfaced as
  asserted.
- Add a competing-alternatives stance: a fingerprint-bound triage disposition on a
  contradiction pair ("rivals; keep both"), recorded in the existing review-state
  store exactly like dismiss and snooze, and honored by every queue.
- Honor that stance, plus a structural-pair exemption for pairs that already carry
  an authored `contradicts` edge or `answers` edges into the same question, in the
  write-time near-duplicate and overlap draft warnings.
- Annotate deep-pack tension pairs with the same `asserted` / `proximity`
  provenance, and include asserted pairs among packed notes even when the sidecar
  is unavailable.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `contradiction-queue`: authored `contradicts` edges become asserted queue entries
  ranked above proximity, every entry carries a provenance label, and a
  competing-alternatives pair stance plus a structural-pair exemption govern the
  write-time proximity warnings.
- `attention-queue`: the ranked review surface preserves asserted-before-proximity
  order, exposes entry provenance in each reason, and treats `competing` as a
  first-class review state honored by every queue.
- `context-packs`: deep-pack tension pairs carry provenance, and authored
  `contradicts` pairs among the packed notes surface without embeddings.

## Impact

The `corpus_contradictions` audit check, the review-state disposition vocabulary,
the attention review-state application, the write-time corpus-aware duplicate and
overlap checks, deep-pack tension assembly, the triage command leaf, and their
focused tests change. A new pure-logic module holds the pair-stance identity and
graph-lookup helpers. No MCP tool docstring, tool parameter set, or relation
registry entry changes, so `tests/fixtures/mcp_tool_schemas.json`,
`src/exomem/tool_surface_contract.json`, and `tests/golden/` stay untouched. No new
external dependency.
