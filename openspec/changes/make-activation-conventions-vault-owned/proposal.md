## Why

The context compiler shipped with four of one owner's conventions compiled into the
server. A vault that names its gear folder `Equipment/`, tracks state in a field called
`stock`, or is written in a language other than English gets a compiler that quietly
finds nothing, with no file to edit and no finding that says why.

Measured on `83e6d5b2`:

- `working_set_resolve.CUE_PATTERNS` and `_CUE_CATEGORIES` are a second cue vocabulary
  held in code. It restates eight of the fourteen roles in `context-roles.yaml` with
  slightly different patterns and category sets. The registry is already
  vault-overridable; this copy is not, so an owner who adds a cue to a role changes
  role selection but not the `category_match` evidence the same cue should produce.
- `working_set_index._page_anchor_kind` admits `resource` anchors only from folders
  named `Products` and `Systems`. No other module uses those names: they are this
  feature's own invention, not a product convention.
- `working_set_index._RAW_MATERIAL_FOLDERS` and `_SKIP_DIR_NAMES` are private copies of
  layout the product already owns (`vault.in_append_only_tree`,
  `vault.VAULT_SCAN_SKIP_DIRS`, `find_corpus.EXCLUDED_DIR_NAMES`). A future change to
  the product's layout authority would leave the compiler behind.
- `working_set_state._STATE_FIELDS` and `_DATE_FIELDS` decide which Records fields state
  an anchor's current condition. They are English field names chosen from one vault.
- `working_set_resolve._STOPWORDS` is an English function-word list.

Exomem's other vocabularies (relations, traversal profiles, source taxonomy, semantic
language, context roles) are reviewed registries the owner extends. Activation
conventions belong with them.

## What Changes

- **One cue vocabulary.** Delete `CUE_PATTERNS` and `_CUE_CATEGORIES`. The resolver
  derives cue → category from the effective role registry: a role whose cue matches the
  turn contributes that role's categories to `category_match`. An owner's `add_cues`
  then reaches both role selection and anchor evidence, in any language.
- **New registry `activation-conventions.yaml`**, shipped in the skill scaffold and the
  plugin copy, overridable at `<Knowledge Base>/_Schema/activation-conventions.yaml`,
  with four sections:
  - `anchors`: which folders, tags and frontmatter `type` values make a page a
    `resource` or a `hub` anchor. The six anchor kinds stay closed; how a vault spells
    membership of them becomes the owner's.
  - `state`: ordered state-field and date-field names for the current-state resolver.
  - `stopwords`: words ignored by the lexical band.
  - `resolution`: the structural thresholds the resolver ships as defaults, starting
    with `rare_term_max_anchors` from `make-anchor-resolution-sound`. A threshold is a
    count the server measures against, never a relevance score.
- **Layout follows the product.** The index walk uses the product's shared skip list and
  `in_append_only_tree` instead of private copies. Raw material stays out of the anchor
  catalogue because it is immutable evidence, wherever the product says it lives.
- **Same load contract as `context-roles.yaml`.** Shipped defaults plus vault override;
  a broken override falls back to shipped and reports findings in
  `generation.conventions_source` and `generation.conventions_findings`; the digest is
  part of the index identity and the packet cache key, so an edit rebuilds the
  disposable index and never serves a stale packet.
- **Bounded by construction.** Patterns are plain substrings, never regular
  expressions. Caps on entries per section; an entry over a cap is dropped with a
  finding the owner can read.
- **The agent can configure it.** The registry is a vault file under `_Schema/`, so an
  agent edits it through the same governed write tools it uses for the other
  registries, and reads back the effective conventions and findings from the packet's
  `generation` block. No server component writes it.

Not in this change: new anchor kinds, server-proposed conventions (that needs the
decision ledger owned by `add-consolidation-dreamer`), making `Entities/`, `Sources/`
or `Evidence/` relocatable (those are product-wide layout used by ten modules, and
belong to a product-level change if wanted).

## Capabilities

### New Capabilities

- `activation-conventions`: the vault-owned registry of anchor membership rules,
  state fields and stopwords that the context compiler reads.

### Modified Capabilities

- `context-activation`: the activation index takes anchor membership from the
  conventions registry and layout from the product's shared authorities.
- `context-roles`: role cues are the single cue vocabulary, feeding `category_match`
  as well as role selection.

## Impact

- Code: `working_set_resolve.py`, `working_set_index.py`, `working_set_state.py`,
  `working_set_runtime.py` (cache key, index identity), new
  `activation_conventions.py` loader, scaffold and plugin registry files.
- Behaviour on shipped defaults: anchor membership, state fields and stopwords are
  byte-equivalent to today. Cue-derived `category_match` changes slightly because the
  role registry's cues are a superset of the deleted table (`budget`, `left`, `lately`,
  `next`, `we already`, plus six roles that had no entry). The deterministic activation
  audit must stay inside its pre-registered bounds; this lands before the benchmark's
  fixture digest freezes.
- Existing `.working-set.sqlite` sidecars rebuild once (index identity now includes the
  conventions digest).
- Sequenced after `activate-context-on-host-turns` merges: both touch
  `working_set_runtime.py`.
