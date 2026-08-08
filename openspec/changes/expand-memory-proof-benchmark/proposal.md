# Expand the memory-proof benchmark toward the writable-knowledge frontier (v0.2)

## Why

> **Scope note (2026-08-09):** the cross-product comparison and
> industry-standard packaging ambitions of this change are superseded by
> `rescope-benchmark-instrument-and-public-suite` after the 2026-08-08
> independent adversarial audit voided the head-to-head in both directions.
> Track B continues under that change's internal-instrument contract; the
> public number moves to the exomem-only `public-suite-eval` capability.
> The falsification register, oracle-derivation rule, and defect ledger
> (section 4b) remain binding and live.

v0.1 proved the method: seven epistemic-mechanics families over a seeded
bitemporal corpus found a real product defect (the degraded-retention gate,
fixed as `91b016f` and measured: factual_qa 0→99/180 lexical, 157/180
embeddings), and the deliberately-absent capability families discriminate as
designed. The declared goal is larger: **maximize coverage of what can be
written down digitally** — conventional memory benchmarks (LoCoMo,
LongMemEval, ConvoMem) measure conversation-recall QA and have stopped
discriminating between products (their own writeups conclude retrieval
near-parity and answerer-bound QA). Nothing public tests procedural
knowledge, negation/counterfactuals, quantitative chains, cross-lingual
facts, preference-vs-fact attribution, source-reliability learning, or
long-horizon corpus entropy. The Polanyi boundary (tacit, unwritable
knowledge) is out of scope by definition; everything writable and
deterministically checkable is in scope, and what is writable but only
human-judgeable routes to the predeclared-rubric track. v0.2 also closes the
one harness gap v0.1 exposed (governance measured against an ungoverned
vault) and adds the packaging discipline an **industry-standard** benchmark
requires: external replication, versioned releases, provider onboarding, and
a publication gate.

Relation to existing changes: extends the still-active
`add-memory-proof-benchmark` (capabilities `memory-proof-corpus`,
`memory-proof-harness`) without modifying its shipped requirements; the
falsification register, oracle-derivation rule, parity reports, and
no-aggregate reporting all continue to bind every new family.

## What Changes

- New scenario-family expansion program in `benchmarks/membench`: an
  explicit family registry with oracle-ability classification
  (deterministic-oracle vs rubric-track vs out-of-scope), and eight new v0.2
  families: procedural/how-to chains, quantitative reasoning over stored
  values, negation & counterfactuals, cross-lingual facts,
  preference-vs-fact attribution, source-reliability learning, long-horizon
  (52-week) entropy, and multimodal depth (real PDF + image OCR + audio
  transcript retrieval under the media-extra profile).
- Governance wiring in the harness: the exomem adapter translates corpus
  `policies.yaml` into the vault's opt-in `_Governance/` policy, the runner
  threads persona identity per query, and the adapter declares
  `GOVERNED_VIEWS` — so Track B's governance family measures the shipped
  governance engine instead of a default-open vault.
- Industry-standard packaging: a replication kit (pinned seeds + one-command
  regeneration + hash verification on a clean machine), versioned corpus
  releases with changelogs, a provider onboarding document (adapter contract,
  native-renderer obligations, default/recommended profile declaration), and
  a predeclared publication gate (judge–human agreement measured before any
  public comparative table; held-out seed reserved for release numbers).

## Capabilities

### New Capabilities
None.

### Modified Capabilities
- `memory-proof-corpus`: family registry + eight v0.2 families + replication
  kit and versioned releases.
- `memory-proof-harness`: governed-views wiring + persona threading +
  provider onboarding + publication gate.

## Impact

- All additive within `benchmarks/` and `tests/test_membench_*.py`; no
  product-runtime changes (the governance wiring drives exomem's existing
  public `govern_memory`/policy surfaces from the adapter — if a genuinely
  missing public seam surfaces, it stops for its own minimal additive
  proposal). Optional/heavy behavior stays default-off with recorded
  degradation: the multimodal-depth family requires the `media` extra and
  degrades to `pdf_unavailable`-style honesty without it; cross-lingual
  content is generated from the same privacy-safe wordbank machinery with
  non-Latin syllabaries and skips scoring where a product declares no
  support (unsupported, never zero). Pure-substrate rule unchanged: any
  model-backed judging remains desk-side, default-off; deterministic gates
  stay final.
