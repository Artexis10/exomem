## Why

The context compiler shipped with one owner's conventions compiled into the server. A
vault that names its gear folder `Equipment/`, tracks state in a field called `stock`,
heads its sections in another language, or is simply denser than the one it was tuned on
gets a compiler that quietly finds less, with no file to edit, no tool to change it and
no finding that says why.

Measured on main at `169e6deb`:

- `working_set_resolve.CUE_PATTERNS` and `_CUE_CATEGORIES` are a second cue vocabulary
  held in code. It restates eight of the fourteen roles in `context-roles.yaml` with
  different patterns and category sets. The registry is vault-overridable; this copy is
  not, so an owner who adds a cue to a role changes role selection and not the
  `category_match` evidence the same cue should produce.
- `working_set_index._page_anchor_kind` admits `resource` anchors only from folders
  named `Products` and `Systems`. No other module uses those names.
- `working_set_index._SKIP_DIR_NAMES` is the index's own skip list, and
  `_RAW_MATERIAL_FOLDERS` restates what `vault.in_append_only_tree` already defines.
- `working_set_index._CATEGORY_BY_LABEL` maps English section headings to categories,
  restating a vocabulary the semantic-language registry already owns and lets a vault
  override.
- `working_set_state._STATE_FIELDS` and `_DATE_FIELDS` are English field names chosen
  from one vault.
- `working_set_index.STOPWORDS` is an English function-word list. It decides which turn
  words the lexical band ignores and which derived short names the index admits.
- `working_set_index.RARE_TERM_MAX_ANCHORS = 3` decides when one shared name word is
  rare enough to count as contact, and which derived short names are distinctive enough
  to admit. Three suits a vault of a few hundred anchors and is a guess for any other.
- Neither `context-roles.yaml` nor any new registry can be written by an agent through
  MCP: `schema_memory` has no subject for them, and on hosted `_Schema/` is refused to
  the generic file tools by design.

Exomem's other vocabularies (relations, traversal profiles, source taxonomy, semantic
language) are reviewed registries the owner extends through `schema_memory`. Activation
conventions belong with them.

This change does not make words in other scripts recognisable; the tokeniser fix on
`make-anchor-resolution-sound` does, and this change depends on it.

## What Changes

- **One cue vocabulary, explicit evidence.** Delete `CUE_PATTERNS` and
  `_CUE_CATEGORIES`. A role declares `evidence_cues` and `evidence_categories`; a cue
  counts as evidence only when it is three characters or longer, tokenises to at least
  one term and matches on term boundaries. The shipped registry carries the deleted
  table's patterns and category sets and never widens them; a question mark alone stops
  counting. A cue an owner adds to a role's `evidence_cues` reaches both role selection
  and anchor evidence.
- **New registry `activation-conventions.yaml`**, shipped in the skill scaffold and the
  plugin copy, overridable at `<Knowledge Base>/_Schema/activation-conventions.yaml`:
  - `anchors`: folders, tags and frontmatter `type` values that make a page a `resource`
    or a `hub` anchor, and the folders the index skips;
  - `state`: ordered state-field and date-field names;
  - `stopwords`: add-only;
  - `resolution`: `rare_term_max_anchors`, which an owner may tighten and not loosen.
- **Page categories from the product's own registry.** Section headings map to
  categories through the semantic-language registry first, the built-in map second.
- **Layout.** Append-only trees and the governance trees come from the product's
  authority; the skip list stays the index's own and becomes a convention; archived
  anchors stay resolvable.
- **Same load contract as `context-roles.yaml`, plus a size cap before parsing**, on both
  loaders. A broken override falls back to shipped and reports findings in `generation`.
- **A conventions edit wipes and rebuilds the sidecar**, changes the packet cache key and
  makes older continuity tokens `stale`.
- **The agent can configure it, on every tier.** `schema_memory` gains the subjects
  `context-roles` and `activation-conventions`: validate, diff, and dedicated
  hash-guarded save operations that take a `why` and refuse a proposal with findings. The generic file tools stay refused for
  `_Schema/`.
- **The evidence rules stay in code.** No file can change which kinds establish contact
  or which combinations resolve.

Not in this change: the tokeniser, segmentation of scripts without word separators, new
anchor kinds, server-proposed conventions, non-distinctive terms derived from the
vault's own counts (a follow-up with its own audit), relocating `Entities/`, `Sources/`
or `Evidence/`.

## Capabilities

### New Capabilities

- `activation-conventions`: the vault-owned registry, its bounds, its effect on the
  sidecar, and its governed write path.

### Modified Capabilities

- `context-activation`: the activation index takes anchor membership and its skip list
  from the conventions registry and raw-material exclusion from the product's authority.
- `context-roles`: role cues are the single cue vocabulary; evidence categories are
  explicit and bounded; the registry gains caps and a governed write path.

## Impact

- Tool surface: `schema_memory` accepts two more subjects. Schema fixtures, digests,
  capabilities and plugin trees are regenerated; connector clients need an action-schema
  refresh. The hosted gateway allows the two subjects.
- Code: `working_set_resolve.py`, `working_set_index.py`, `working_set_state.py`,
  `working_set_runtime.py` (cache key, `generation`, continuity payload),
  `context_roles.py` (evidence categories, caps, size cap, save), new
  `activation_conventions.py`, `commands.py` (`schema_memory`), `hosted_gateway.py`,
  scaffold and plugin registry files.
- Behaviour on shipped defaults: resolution, state and packets equal today's, with three
  intended differences: `Products` and `Systems` match case-insensitively, pages inside
  the governance trees are never anchors, and a question mark alone no longer makes
  `question` and `problem` eligible. Equivalence and property tests
  pin it; the deterministic audit gates recall and precision before and after.
- Existing `.working-set.sqlite` sidecars rebuild once.
- Builds on `activate-context-on-host-turns` and `make-anchor-resolution-sound`, both
  merged, and on the tokeniser fix in the latter's tasks 4a.
