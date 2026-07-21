## Context

`add-first-class-semantic-language` establishes compact observations, governed
rich kinds, a shared semantic evaluator, and posthoc reconciliation. The runtime
already parses `- [category] content #tags (context) ^anchor`, but the authoring
experience still has four gaps:

1. compiled-note templates emphasize prose headings and do not require an
   `## Observations` section;
2. active compiled pages can reach disk with zero usable semantic units;
3. a recognized rich heading currently ends at every following ATX heading, so
   a parent with nested subsections can become an empty unit; and
4. bootstrap, tool descriptions, scaffold skills, and packaged plugin skills can
   explain different subsets of the language.

This change is a follow-on to `add-first-class-semantic-language`, not an
alternative ontology. Category remains open vocabulary, compact-unit kind remains
`observation`, rich kinds remain governed, and the shared evaluator remains the
single write boundary. Implementation must be based on a branch where that
change's code and specs are available.

The distributable repository must remain generic. No private vault is an input to
runtime contract generation, tests, examples, docs, plugin sync, or default skill
packaging. Maintainer-only personal skill archives remain a separate explicit
overlay path and are not an implementation or test dependency.

## Goals / Non-Goals

**Goals:**

- Make at least one valid, non-empty semantic unit a real invariant of every new
  active compiled note, with compact `[category]` observations as the default
  lightweight form.
- Preserve rich units for content needing a governed epistemic kind or typed
  unit relations.
- Apply one deterministic result across every in-process path that can create,
  replace, or activate compiled notes, including Tier 2.
- Give MCP-only and plugin/skill clients enough local instruction to author valid
  notes with no repository or private-vault context.
- Prevent drift between the runtime contract and shipped human-readable
  projections.
- Preserve legacy and direct-editor content without automatic rewriting.

**Non-Goals:**

- Closing the category vocabulary or inferring an epistemic kind from category.
- Requiring a fixed observation count above one, auto-generating observations,
  or judging whether their claims are true or complete.
- Rewriting existing pages, repairing a particular vault, or placing private
  content in fixtures.
- Replacing prose sections, rich semantic units, typed relations, or page-level
  frontmatter.
- Restricting arbitrary Markdown, templates, dataset cards, Sources, Evidence,
  indexes, or other non-compiled documents merely because they use Tier 2.
- Adding a model. Parsing, validation, rendering, and drift checks remain
  deterministic pure-substrate operations.

## Decisions

### 1. Every active compiled note has at least one usable semantic unit

A new active compiled page is authoring-compliant only when the shared parser
returns at least one valid, non-empty normalized semantic unit. Either form can
satisfy the invariant: a compact observation under canonical
`## Observations`, or a rich block with a governed kind and substantive body.
The six compiled types are `research-note`, `insight`, `failure`, `pattern`,
`experiment`, and `production-log`.

The minimum is one, not a heuristic quota. Compact is the preferred concise form
and the default in templates because it makes open `[category]` vocabulary
visible at the authoring moment. Rich form is used when governed epistemic kind,
metadata, or typed unit relations matter. A rich unit already has a category,
defaulting to its kind unless explicit `category` metadata overrides it, so a
duplicate compact restatement is neither required nor generated.

Inactive states follow the lifecycle rules already defined by the semantic write
contract. They may be saved without a unit, with a warning, but transition to
active must satisfy the invariant. A replacement creates a new active successor
and therefore must satisfy it even when the predecessor is grandfathered.

Alternative considered: require a compact observation in addition to any rich
unit. Rejected because it conflicts with the unified two-form ontology and would
create duplicate semantic/index content. Alternative considered: require three
to five units. Rejected because the server cannot determine an epistemically
correct count without reasoning and small notes can be complete with one.

### 2. Enforcement stays in the shared semantic evaluator

The new finding is computed from the normalized after-state in the shared
semantic contract. It is not implemented as a route-specific check. Full
precommit enforcement applies to active compiled creates, replacements,
adoption compilation, edit/activation transitions, and Tier-2
create/overwrite/append under compiled paths. `validate_only` returns the same
finding without mutation.

Classification uses two exact steps. First,
`compiled_intent(after_state) = canonical_compiled_destination(path) OR
normalized_type in COMPILED_TYPES`. The existing canonical route resolver maps
the six type roots (`Notes/Research`, `Notes/Insights`, `Notes/Failures`,
`Notes/Patterns`, `Notes/Experiments`, and `Notes/Productions`) while applying its
index/hub/snapshot exclusions. Structural validation then rejects canonical-route
content with a missing/wrong compiled type and recognized compiled types at a
noncanonical destination.

Only after that match passes does `requires_semantic_unit(after_state)` become
true: the result is writable managed Markdown, outside Sources/Evidence/trash,
has matched compiled intent, has no existing activation exclusion, and its
resolved lifecycle is active. Inactive statuses (`draft`, `planned`, `dropped`,
`archived`, and `superseded`) do not require a unit until an active transition.

The predicate does not blanket-block `create_file`. Indexes, logs, schema/admin
artifacts, templates, dataset cards, hubs, snapshots, Sources, Evidence,
non-Markdown, and arbitrary non-compiled Markdown retain existing structural and
safety rules. Personalized skill packaging is outside the runtime write
predicate. Every facade gets the same stable finding, source location where
available, and remediation.

For existing pages, the activation manifest remains the compatibility boundary.
A grandfathered page with no semantic unit can receive unrelated guarded edits
with visible debt, but a currently compliant page cannot remove its final valid
unit. A move preserves grandfathering and does not manufacture a new-unit
obligation. External editor changes are never reverted: watcher
and reconcile exclude invalid units from derived indexes and surface the debt
through audit/review.

Alternative considered: forbid Tier-2 Markdown writes anywhere under `Notes/`.
Rejected because Tier 2 has legitimate non-compiled uses and the shared
after-state evaluator already provides the correct semantic boundary.

### 3. Rich blocks follow Markdown heading hierarchy

A recognized rich heading with numeric ATX level `N` owns content through the
line before the next non-fenced ATX heading whose numeric level is `<= N`.
Headings with numeric level `> N` are deeper and are retained
as body content, even when their labels are themselves recognized kinds. Unknown
structural parents can contain recognized rich children. Authors who intend
sibling rich units must use sibling heading levels.

After leading metadata, relation rows, descendant heading markers, and whitespace
are removed, a rich block with no substantive body is invalid. Parsing emits a
stable `empty_rich_unit` diagnostic and no indexable semantic unit. Applicable
precommit writes fail; posthoc parsing preserves the Markdown and surfaces the
finding. Normalized unit spans never overlap: a nested recognized heading or
compact-shaped bullet inside an open rich block remains body content, not a
second unit. Authors place compact units under a separate `## Observations`
section when they intend independent compact units.

Alternative considered: remove plural heading aliases. Rejected because it
would silently change existing rich-unit recognition. Alternative considered:
require a new explicit rich-block marker. Rejected as unnecessary migration and
authoring overhead; heading hierarchy already expresses ownership.

### 4. One structured contract feeds every runtime projection

Add a small versioned semantic-authoring contract with a deterministic content
digest in package code. It contains, at minimum:

- the exact compact syntax and canonical section;
- open category versus governed kind semantics;
- the minimum-unit applicability predicate and explicit exemptions;
- rich syntax and heading-boundary behavior;
- path-independent stable error codes and remediation; and
- the preferred typed routes (`remember`, `replace_memory`, and
  `observe_memory`) plus the Tier-2 compatibility rule.

Bootstrap returns this object directly in `compact`, `full`, and `diagnostics`
profiles. The command registry uses a deterministic concise renderer in the
descriptions for `remember`, `replace_memory`, `observe_memory`, applicable
`edit_memory` transitions, and the create/overwrite/append behavior of
`manage_memory_file`; generated REST, CLI, OpenAPI, and capability docs inherit
those descriptions.

The hand-authored scaffold remains the canonical source for the complete skill.
Its bounded semantic-authoring section is updated directly and checked against a
deterministic Markdown rendering of the runtime object. Plugin, filesystem, and
uploadable skill packaging continue to derive from the generic scaffold. Every
separately installable workflow archive that can compile, replace, or curate an
active note embeds the complete concise minimum contract, while plugin-bundled
workflows may also link to the bundled core skill. A missing core dependency can
never be hidden behind a broken reference. No client must consult repository
instructions or bootstrap to interpret an installed workflow skill.

Alternative considered: maintain equivalent prose independently on every
surface. Rejected because that is the drift that produced the current gap.

### 5. Templates teach the invariant at the writing moment

Every active compiled-note documentation template shows a valid compact example
inside a fenced example, where the parser cannot count it. Every generated
candidate adds `## Observations` with a deliberately non-parseable fill-in row;
an untouched candidate therefore fails `missing_semantic_unit`. Structural prose
sections remain available, and rich blocks are shown only when their governed kind is intended.
Authoring-tool descriptions put the syntax in the `content` parameter guidance,
not only in a distant guide. Validation remediation includes a minimal generic
example with no suggested closed category list.

Examples use inert, generic domains and are covered by the existing scaffold
leak guard plus expanded distribution checks. The expanded gate scans public
source, plugin/marketplace, docs, OpenSpec, tests/fixtures/examples, generated
schema/docs, and unpacked wheel/sdist/skill/plugin artifacts. Unknown or binary
members require explicit provenance handling; diagnostics identify only rule,
file, and line, never matched content. Tests construct synthetic pages; no live
or production corpus is read, copied, transformed, or tokenized into the
repository. Explicit personalized packaging remains allowed only to untracked
private output and cannot feed a public build.

## Risks / Trade-offs

- **Existing integrations create active notes with no semantic units** → `validate_only`
  returns the exact finding and remediation; release notes call out the tightened
  invariant; inactive drafts remain possible.
- **A structural heading is accidentally treated as a rich unit** → only a
  recognized, non-empty rich block counts; templates still default to explicit
  compact observations.
- **Hierarchy-aware parsing changes rich fingerprints/spans** → bump the parser /
  contract version, rebuild derived indexes, add nested-heading and stale-index
  migration tests, and never rewrite Markdown.
- **Tool descriptions become too large** → render a concise contract for schemas
  and keep expanded examples in bootstrap `full` and bundled skills; the compact
  form still includes every normative rule.
- **Human-readable projections drift** → compare normalized contract renderings
  plus version/content digest in CI; unpack public artifacts; and retain the
  existing generated plugin-tree sync gate.
- **A broad privacy scan creates false positives or leaks its own match** → use
  format-aware generic rules and provenance, report only rule/file/line, and
  never encode a particular private corpus or identifier as a public fixture or
  allowlist.

## Migration Plan

1. Land or rebase onto `add-first-class-semantic-language` and preserve its
   activation-manifest compatibility boundary.
2. Introduce the canonical contract object, version and digest it, and add
   projection, standalone-distribution, unpacked-artifact, and privacy tests
   before changing enforcement.
3. Make rich parsing hierarchy-aware, bump parser/index schema versions as
   required, and verify a rebuild changes only derived state.
4. Add the minimum-semantic-unit invariant to the shared evaluator and wire stable
   findings through every creation/activation path.
5. Update templates, scaffold/workflow skills, plugin packaging, bootstrap, tool
   descriptions, generated docs, and fixtures from the same contract.
6. Run focused parser/write/bootstrap/surface/package/privacy tests, the lean
   suite, strict OpenSpec validation, and an independent contract/privacy review.

Rollback removes the new precommit finding and restores the prior parser version.
No Markdown rollback or content migration is needed because the change only
rejects invalid new in-process writes and rebuilds derived state.

## Open Questions

None. The minimum, lifecycle boundary, hierarchy rule, standalone distribution
boundary, and privacy boundary are decided by this change.
