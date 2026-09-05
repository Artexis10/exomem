## Why

Exomem already has a governed, extensible relation registry, but the ordinary authoring loop does not expose it well enough: agents fall back to `relates_to`, recurring unknown labels become invalid-looking proposal skeletons, and an accepted extension invalidates the graph without arranging prompt re-resolution. The relation-review queue compounds the gap by scanning and parsing the vault twice and potentially running one corpus-wide embedding search per page; a live one-item request took about 41 seconds under normal multi-agent load.

## What Changes

- Add an agent-facing relation-intent resolver that searches the complete core vocabulary, aliases, inverses, extension metadata, replacement history, and indexed unregistered usage while leaving semantic adequacy to the calling agent.
- Keep extension keys namespaced internally while using aliases as clean authoring labels; provide a proposal-first, hash-guarded relation save flow that does not require callers to rewrite the whole registry by hand.
- Teach bootstrap, the generic scaffold, and compact mutation responses to prefer the most specific truthful registered relation, preserve `relates_to` and no edge as honest outcomes, and route unknown labels into governed resolution.
- Have the canonical batch writer automatically inject a durable full-scope graph epoch/checkpoint for accepted registry YAML, then converge through one live/deferred dispatcher that rebinds when proofs hold and otherwise rebuilds, without holding the Markdown writer boundary for derived work.
- Make active terminal-survivor queries include deprecated predecessor observations while deprecated-key queries remain historically exact; preserve immutable immediate replacement links, terminal-survivor diagnostics, and every observation's raw/canonical identity instead of treating broad successors as symmetric equivalents.
- Replace the interactive relation queue's Markdown census and per-page cosine work with one bounded graph-native snapshot; new accept and triage decisions require a source hint and revalidate one page, while hintless legacy decisions receive only bounded-prefix compatibility or an explicit refresh result.
- Add synthetic whole-lifecycle, isolation, historical-census, graph-value, and realistic concurrency regressions. No private vault content enters fixtures or reports.

## Capabilities

### New Capabilities

- `relation-vocabulary-evolution`: Deterministic intent resolution, reviewed vault-local relation proposals, ergonomic guarded persistence, precise-by-default authoring guidance, replacement-aware reuse, and bounded relation review under concurrency.

### Modified Capabilities

- `epistemic-relation-registry`: Recurring unknowns become usable but explicitly incomplete promotion candidates; guarded relation changes gain clean aliases, duplicate-review evidence, and graph activation semantics.
- `epistemic-graph`: Registry changes rebind derived edge identity, and relation-review candidate assembly becomes one bounded indexed snapshot rather than a corpus walk.
- `semantic-write-contract`: Compact successful mutation terminals surface bounded unknown-relation guidance across every semantic write route without making an unknown edge qualifying.
- `agent-bootstrap-contract`: Every client tier discovers the active clean relation vocabulary and the resolve, propose, honest-fallback, and deprecation workflow.
- `epistemic-review-studio`: Relation-queue loading, accept, and triage preserve server ordering and source hints and render warming/truncation honestly.
- `graph-value-benchmark`: Synthetic cases measure extension activation, exact-versus-parent filtering, duplicate prevention, semantic abstention, and bounded concurrent review.
- `command-surface`: Relation resolve/propose/save, census scoping, source-hinted review decisions, selector classification, and generated MCP/REST/CLI schemas remain one shared contract.

## Impact

The change affects relation registry/inference, graph schema and synchronization, relation-filter expansion, relation queue assembly, mutation terminal projection, bootstrap/scaffold guidance, Studio queue calls, generated MCP/REST/CLI schemas, and the graph-value benchmark. It adds indexes and a derived-sidecar schema bump but no graph database, server-side reasoning model, mandatory embedding call, Markdown migration, automatic ontology mutation, or automatic legacy backfill. Existing registry YAML, raw relation labels, canonical extension keys, aliases, and the legacy full-proposal save path remain readable. New relation-review decisions carry source hints; a hintless legacy ref is searched only in the bounded current queue prefix and otherwise becomes refresh-required rather than triggering a corpus scan. Existing canonical relation meanings are immutable in place: aliases may be added and definitions may be deprecated/replaced, but semantic changes require a new canonical key.
