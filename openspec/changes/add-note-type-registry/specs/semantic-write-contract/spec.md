## MODIFIED Requirements

### Requirement: Compiled Intent And Minimum-Unit Applicability Are Exact

The shared semantic write boundary SHALL define
`compiled_intent(after_state)` as exactly
`canonical_compiled_destination(path) OR the normalized type's note-type role is compiled`.
The effective note-type registry SHALL supply each type's note-type role and each
compiled type's folder. The shipped pack gives role `compiled` to `research-note`,
`insight`, `failure`, `pattern`, `experiment`, and `production-log`, and a vault MAY
register more. The canonical destination resolver SHALL map each compiled type to
its registered folder; the shipped pack sets `Notes/Research`, `Notes/Insights`,
`Notes/Failures`, `Notes/Patterns`, `Notes/Experiments`, and `Notes/Productions`,
respectively. The resolver SHALL apply the existing index, log, schema/admin,
template, dataset-card, hub, snapshot, and activation exclusions. Structural
validation SHALL reject a canonical compiled destination with missing/wrong compiled
type and SHALL reject a recognized compiled type at a noncanonical destination
before applicability is evaluated.

The boundary SHALL then expose one deterministic
`requires_semantic_unit(after_state)` predicate, separate from relation
disposition. It SHALL be true only when compiled intent has passed that
path/type match; the result is writable Markdown inside the managed governed
subtree and outside Sources, Evidence, and trash; no existing activation
exclusion applies; and its admitted effective page status has class `live`. A canonical shipped
label resolves independently of private extensions. An unfamiliar label requires
admitted registry classification; unknown labels remain live with debt.
Absent or empty status retains the public live default without overlay admission or debt.
Unavailable classification SHALL refuse dependent validation and SHALL NOT be
interpreted as an inactive page or a false minimum-unit obligation.

#### Scenario: Typed and Tier-2 writes agree
- **WHEN** equivalent active compiled Markdown is submitted through `remember` and through Tier-2 create at its governed compiled destination
- **THEN** both use `requires_semantic_unit` and return the same semantic-authoring findings

#### Scenario: Tier-2 overwrite and append evaluate the result
- **WHEN** Tier-2 overwrite or append would leave an applicable active compiled page with no valid unit
- **THEN** precommit evaluates the complete resulting document, refuses it, and leaves Markdown and derived state unchanged

#### Scenario: Compiled path cannot bypass with bad frontmatter
- **WHEN** Tier 2 targets a canonical compiled-note route with missing, invalid, or mismatched compiled frontmatter
- **THEN** structural validation fails before commit instead of classifying the page as arbitrary Markdown

#### Scenario: Non-compiled Tier-2 documents are exempt
- **WHEN** Tier 2 writes an index, log, schema/admin artifact, template, dataset card, hub, snapshot, Source, Evidence artifact, non-Markdown file, or arbitrary non-compiled Markdown
- **THEN** existing structural and safety rules apply and the minimum-unit predicate is false

#### Scenario: Validation is non-mutating
- **WHEN** an applicable draft with no valid unit is submitted through a creation path with `validate_only=true`
- **THEN** the response contains `missing_semantic_unit` and no page, index, log, project registration, review state, or auxiliary artifact is written

#### Scenario: Unit and relation obligations stay separate
- **WHEN** a page has a valid semantic unit but no current relation-review disposition, or has a qualifying relation but no valid unit
- **THEN** each independent obligation reports its own finding and neither satisfies the other

#### Scenario: Registered pending labels share the inactive minimum rule
- **WHEN** a normalized compiled after-state uses an admitted status label with class `pending`
- **THEN** its minimum-unit applicability is false under the shared predicate
- **AND** existing structural validation and route-specific field rules remain in effect

#### Scenario: Unavailable extension classification cannot approve a dependent write
- **WHEN** an unfamiliar label requires private status definitions that the caller cannot admit
- **THEN** dependent semantic validation reports classification unavailable and cannot approve the mutation
- **AND** independent operations without that dependency remain usable

#### Scenario: A vault-registered compiled type has compiled intent
- **WHEN** the admitted note-type registry gives a vault-defined type note-type role `compiled` and folder `Notes/<Name>`
- **THEN** a page of that type in that folder has compiled intent and follows the shared minimum-unit predicate
- **AND** the same type in another compiled folder fails structural validation

### Requirement: Observe-Memory Refusals Distinguish Legacy Schema From Policy

When `observe_memory` refuses a target because it is not a writable compiled
page, it SHALL keep the existing refusal code
(`OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`, `FRONTMATTER_REQUIRED` for a
frontmatterless target, or `OUTSIDE_GOVERNED_ROOT` for a target whose real
page lives outside the governed Knowledge Base root) and SHALL distinguish
the cause in the accompanying `remediation` and `resolution` fields rather
than leaving the caller to improvise:

- An untyped page (frontmatter present, `type` missing or without note-type
  role `compiled`), a frontmatterless page, or a page outside the governed
  Knowledge Base root SHALL receive a `remediation` sentence that names the
  concrete safe move — the exact `suggested_path`, the `part_of` target, the
  in-place alternative, and the migration pointer, in that order — plus a
  structured `resolution` of shape `{"action": "create-dated-child",
  "suggested_path": <string>, "link": {"relation": "part_of", "target":
  <legacy page path>}, "in_place": <string>, "migration": <string>}`.
  `suggested_path` SHALL colocate in the legacy page's own directory when
  that page is under the governed Knowledge Base root, and SHALL otherwise
  resolve under `Notes/Research/<parent-dir-slug>/` inside the Knowledge
  Base. `suggested_path` SHALL name a path that does not already exist,
  de-collided the same way page creation de-collides a filename.
- A target whose access tier is not read-write (readonly, excluded, or any
  other non-read-write tier) SHALL receive the same code, a `remediation` that
  names the tier and states the page is policy-protected, and no `resolution`.
- This requirement SHALL NOT widen note-type role `compiled`. `entity`
  remains structural, not compiled.
- No existing error code SHALL be renamed, and no new error code SHALL be
  introduced to carry this distinction.

#### Scenario: Untyped page inside the Knowledge Base offers a dated-child resolution
- **WHEN** `observe_memory` targets a page with frontmatter but no compiled
  `type`, under `Knowledge Base/`
- **THEN** it refuses with `OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`
- **AND** the raised error's `remediation` sentence names the exact
  `suggested_path` and the `part_of` target, and its `resolution.suggested_path`
  is a dated child in the same directory with `link` targeting the legacy
  page at relation `part_of`

#### Scenario: Frontmatterless page gets the same shape of answer
- **WHEN** `observe_memory` targets a page with no frontmatter delimiters
- **THEN** it refuses with `FRONTMATTER_REQUIRED`
- **AND** the error carries the same shape of `remediation` and `resolution`
  as an untyped page, without synthesizing frontmatter on the page itself

#### Scenario: A page outside the Knowledge Base root gets the same shape of answer
- **WHEN** `observe_memory` targets a page whose real file lives outside the
  governed Knowledge Base root
- **THEN** it refuses with `OUTSIDE_GOVERNED_ROOT`
- **AND** the error carries the same shape of `remediation` and `resolution`
  as an untyped page, with `resolution.suggested_path` under
  `Notes/Research/<parent-dir-slug>/` inside the Knowledge Base and `link`
  targeting the outside page at relation `part_of`

#### Scenario: Policy-protected tier gets tier remediation and no resolution
- **WHEN** `observe_memory` targets a page whose access tier is `readonly` or
  `excluded`
- **THEN** it refuses with `OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE`
- **AND** the `remediation` names the tier and states the page is
  policy-protected
- **AND** no `resolution` is present

#### Scenario: A pre-existing same-dated child shifts the suggested path
- **WHEN** a page already exists at the path `suggested_path` would otherwise
  name
- **THEN** the offered `suggested_path` is de-collided (the `-2` form, or
  further, exactly as ordinary page creation de-collides a filename) rather
  than naming a path that already exists

#### Scenario: Compiled-page type set is not widened
- **WHEN** this requirement's resolution logic runs
- **THEN** the set of types with note-type role `compiled` is unchanged and
  `entity` remains outside it
