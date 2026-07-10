## ADDED Requirements

### Requirement: Built-in deterministic traversal profiles
The system SHALL provide immutable built-in `epistemic`, `provenance`, `causal`,
`decision`, and `all` profiles. Each profile SHALL define relation families,
edge directions, extension-parent expansion, deterministic priority, and bounded
defaults. Omitting a profile SHALL preserve current broad context behavior by
using `all`.

#### Scenario: Epistemic lens excludes unrelated operational edges
- **WHEN** context is requested with `traversal_profile="epistemic"`
- **THEN** support, contradiction, refinement, duplication, supersession,
  question, and answer families are traversed within bounds
- **AND** unrelated implementation or generic link edges are excluded and the
  resolved profile is named in the response

### Requirement: Governed custom traversal profiles
The system SHALL load optional custom profiles from a governed YAML file under
`Knowledge Base/_Schema/`. A custom profile MUST extend one built-in profile and
MAY add/remove registered families or exact keys, set direction and deterministic
priority, choose parent-extension expansion, and lower depth/node/edge defaults.
It MUST NOT exceed server hard caps, redefine relations, include unknown edges in
normal mode, or form inheritance cycles.

#### Scenario: Project profile specializes provenance safely
- **WHEN** a valid custom profile extends `provenance` and adds a registered
  domain evidence relation
- **THEN** context includes that extension while retaining the built-in
  provenance bounds and relation semantics

#### Scenario: Invalid profile fails without affecting context
- **WHEN** a custom profile names an unregistered key or requests caps above the
  server maximum
- **THEN** validation reports stable path/span findings and the invalid profile
  is not selected or persisted

### Requirement: Runtime filters only narrow a selected profile
Explicit relation filters SHALL intersect the selected profile. Runtime depth
and node/edge caps SHALL be clamped by server maxima and MAY further narrow the
profile. The system MUST NOT let an explicit filter silently expand a profile or
include an unregistered relation.

#### Scenario: Explicit filter narrows causal context
- **WHEN** the causal profile is selected with `relation_types=["causes"]`
- **THEN** only registered `causes` edges and permitted child extensions are
  traversed, subject to the profile and global caps

### Requirement: Traversal profiles do not alter stored knowledge or ranking
Profile resolution and traversal SHALL be deterministic and read-only. Profiles
MUST NOT write relations, accept suggestions, infer transitive edges, assign
truth/confidence, or change default `find` ordering. Priority SHALL only choose
which otherwise eligible edges fit inside a bounded context response.

#### Scenario: Two lenses leave the corpus unchanged
- **WHEN** the same seed is queried through epistemic and provenance profiles
- **THEN** the returned edge sets may differ, both responses report their lens
  and truncation, and no Markdown, registry, or stored graph edge changes
