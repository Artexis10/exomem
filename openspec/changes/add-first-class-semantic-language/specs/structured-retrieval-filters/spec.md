## ADDED Requirements

### Requirement: Namespaced Page And Unit Filter Contract
Recall SHALL accept a `filters` JSON object over explicit page and unit namespaces. Reserved page system fields SHALL use fixed names such as `page.status`, `page.project`, `page.updated`, and `page.file_type`. Arbitrary frontmatter SHALL use `page.frontmatter:/<RFC-6901-pointer>` with standard `~0`/`~1` escaping. Pointers SHALL traverse runtime mappings only; arrays SHALL be terminal scalar collections, and encountering an array before exhausting the pointer SHALL make that candidate a nonmatch. Numeric pointer segments SHALL remain valid mapping keys when the runtime value is a mapping. Mappings MAY be traversed or tested for existence but MUST NOT be equality-compared. `unit.<field>` SHALL be restricted to `category`, `category_key`, `kind`, `tags`, `context`, and `form`. Unit results SHALL evaluate the complete expression against one parent/unit pair. Page results SHALL evaluate it existentially over one child unit at a time, with a missing-unit sentinel for page-only branches, so separate units cannot jointly satisfy one expression. A page-only expression SHALL preserve page-level `auto` behavior; any `unit.*` predicate SHALL make `result_level="auto"` resolve to unit level.

#### Scenario: Nested page metadata is filterable
- **WHEN** recall uses `{"page.frontmatter:/priority":{"$gte":3}}`
- **THEN** only pages whose numeric frontmatter priority is at least three are eligible

#### Scenario: Frontmatter keys are unambiguous
- **WHEN** a nested or literal-slash frontmatter key is addressed through an escaped RFC 6901 pointer
- **THEN** the compiler resolves exactly that key and does not reinterpret it as a system field or different nesting

#### Scenario: Pointer traversal is runtime-type deterministic
- **WHEN** `/foo/0` encounters mapping key `"0"` on one page and an array at `foo` on another
- **THEN** the mapping candidate resolves normally, the array candidate is a nonmatch, and no corpus-dependent compile-time interpretation occurs

#### Scenario: Unit predicate changes automatic result level
- **WHEN** recall omits `result_level` and uses `{"unit.category":{"$eq":"rule"}}`
- **THEN** `auto` returns rule units with their parent citations

#### Scenario: Page mode requires one fully matching unit
- **WHEN** page-level recall filters a unit category and unit tag together
- **THEN** a parent is eligible only when the same semantic unit satisfies both predicates

#### Scenario: Page-only OR branch matches a page without units
- **WHEN** a page with no units satisfies the page side of an OR expression whose other side is a unit predicate
- **THEN** the page result may match through the page branch without fabricating a child unit

### Requirement: Typed Bounded Operators And Logic
Leaf predicates SHALL support `$eq`, `$ne`, `$in`, `$all`, `$contains`, `$exists`, `$gt`, `$gte`, `$lt`, `$lte`, and inclusive `$between`; multiple operators on one field SHALL combine with AND. `$eq`/`$ne` SHALL accept scalar or null operands and compare only scalar/null fields with exact type identity. A runtime array or mapping SHALL be a nonmatch for `$eq`/`$ne`; using those operators on a reserved/unit field known to be an array SHALL be a validation error. `$in` SHALL mean scalar membership or scalar-array overlap, `$contains` SHALL mean exact scalar-array membership or substring for an explicitly string-valued field, and `$all` SHALL require every requested scalar in the terminal array; operand order and duplicates SHALL NOT affect these array results. `$in`, `$all`, `$and`, and `$or` arrays SHALL be non-empty, `$between` SHALL contain exactly two ordered values, and `$not` SHALL contain exactly one expression. Ordered comparison SHALL accept only scalar numbers, ISO dates, or timezone-qualified RFC 3339 date-times of the same resolved type; date-times SHALL normalize to UTC and date/date-time types SHALL NOT mix. Every comparison against a missing field or incompatible runtime type, including `$ne`, SHALL be false except `$exists:false`; logical `$not` SHALL remain the true complement. `$and`, `$or`, and `$not` SHALL compose expressions with a maximum nesting depth of four and a maximum of 32 leaf predicates. The encoded `filters` JSON and both structurally normalized and alias-resolved combined filter plans, including compiled shortcuts, SHALL each be at most 16 KiB; each RFC 6901 pointer SHALL be at most 512 UTF-8 bytes and 16 decoded segments; each string operand or shortcut value SHALL be at most 1,024 Unicode code points and 4,096 UTF-8 bytes; each `$in`/`$all` operand and shortcut list SHALL contain at most 64 scalar values and all scalar collection plus shortcut values together at most 256 values; numeric operands SHALL be finite JSON numbers with at most 64 encoded characters. Raw counts SHALL apply before deduplication or alias resolution. Values SHALL NOT be silently coerced, and regex, SQL, scripts, or unknown operators SHALL be rejected. Raw/structural validation and resource limits SHALL run before alias resolution; resolved-size validation SHALL run before backend access or candidate generation.

#### Scenario: Logical composition is deterministic
- **WHEN** a filter combines active status with an OR of two project keys
- **THEN** the normalized expression preserves that grouping and returns only active pages in either project

#### Scenario: Between is inclusive and typed
- **WHEN** recall filters `page.updated` with `{"$between":["2026-01-01","2026-01-31"]}`
- **THEN** both boundary dates are eligible and a non-date value produces a validation error

#### Scenario: Array operators remain distinct
- **WHEN** one page has tags `["auth", "oauth"]`
- **THEN** `$contains:"auth"` and `$all:["auth","oauth"]` match, while `$all:["auth","billing"]` does not

#### Scenario: Scalar equality does not overload array membership
- **WHEN** a heterogeneous frontmatter field is scalar on one page and a scalar array on another and the filter uses `$eq`
- **THEN** exact scalar/null equality is evaluated on the scalar page, the array page is a nonmatch, and callers use `$contains`, `$all`, or `$in` for arrays

#### Scenario: Complexity limits fail before retrieval
- **WHEN** a caller submits a fifth nested logical level or more than 32 leaf predicates
- **THEN** recall fails with a bounded-filter validation code and performs no candidate search

#### Scenario: One leaf cannot hide an unbounded operand
- **WHEN** a filter exceeds the byte, pointer, string, collection, total-value, or numeric limit while remaining under 32 leaves
- **THEN** recall rejects it before alias resolution or candidate work and `explain=true` never echoes the oversized payload

#### Scenario: Missing differs from not-equal
- **WHEN** one page omits `owner`, another has `owner: null`, and another has `owner: sam`
- **THEN** `$ne:"sam"` matches none of those three because missing and null are not same-type unequal strings, `$exists:false` matches only the missing value, and `$eq:null` matches only explicit null

### Requirement: Shortcuts Compile Into The Same Filter Plan
Existing recall shortcuts for types, projects, tags, speakers, file types, dates, categories, and kinds SHALL retain their documented behavior and compile into the same normalized filter plan as `filters`. Every shortcut list and value SHALL obey the same per-list, per-value, combined-value, raw-count-before-deduplication, and normalized-plan bounds as generic collection operands. Values within a shortcut list SHALL remain ORed; independent shortcuts, the generic expression, and query text SHALL combine with AND. Registry category aliases SHALL resolve before comparison. Supplying an empty text query with any valid shortcut or generic filter SHALL perform filtered-most-recent retrieval.

#### Scenario: Shortcut and generic filter intersect
- **WHEN** recall supplies `projects=["alpha"]` and `{"page.status":{"$eq":"active"}}`
- **THEN** only active pages in project alpha are eligible

#### Scenario: Filter-only retrieval needs no dummy query
- **WHEN** query text is empty and the filter selects `page.status="active"`
- **THEN** matching pages are returned in the documented filtered-most-recent order

#### Scenario: Category alias is resolved consistently
- **WHEN** a saved category alias maps `configuration` to `config` and a filter asks for `unit.category="configuration"`
- **THEN** the normalized plan records the resolution and selects canonical `config` units without changing their authored category keys

#### Scenario: Shortcut list cannot bypass resource bounds
- **WHEN** a project, tag, speaker, file-type, category, kind, type, or date shortcut exceeds its list/value limit or pushes the combined plan over its total limit
- **THEN** recall rejects it before alias resolution or candidate work under the same bounded-filter error contract

### Requirement: Invalid Filters Fail Closed And Identically
The filter compiler SHALL validate namespace, RFC 6901 pointer, reserved field, operator, value arity, and resolved value type before candidate generation. Errors SHALL identify the JSON path, stable code, expected shape/type, and remediation. Missing frontmatter values SHALL differ from explicit null and SHALL be testable through `$exists`. Reserved page/unit fields SHALL reuse existing shortcut canonicalization/case semantics; arbitrary frontmatter strings SHALL compare exactly after YAML parsing. Access scope and excluded-subtree policy SHALL apply before caller filters and MUST NOT be weakened by them. Every retrieval backend and facade SHALL consume the same normalized typed filter AST; an unsupported backend capability MUST fail or use a proven equivalent post-filter before ranking and MUST NOT silently broaden results.

#### Scenario: Misspelled field does not look like an empty result
- **WHEN** a caller uses the unknown closed unit field `unit.categry`
- **THEN** recall returns an invalid-filter-field error pointing to that path rather than zero hits

#### Scenario: Missing and null remain distinct
- **WHEN** one page omits `owner` and another declares `owner: null`
- **THEN** `$exists:false` matches only the missing field while `$eq:null` matches only the explicit null

#### Scenario: Backend parity is enforced
- **WHEN** the same structured filter runs through lexical, vector, hybrid, graph-enriched, SQLite, and any optional search backend
- **THEN** every lane starts from the same eligible identity set and no backend returns a filtered-out candidate

#### Scenario: Heterogeneous scalar and array fields stay backend-identical
- **WHEN** equivalent `$eq`, `$in`, `$contains`, and `$all` predicates run over a field that is scalar, array, null, missing, or a mapping on different pages
- **THEN** every backend applies the same exact-type, terminal-array, and nonmatch rules

#### Scenario: Numeric mapping key is not mistaken for array traversal
- **WHEN** the same RFC 6901 pointer segment `0` follows a mapping in one candidate and an array in another
- **THEN** every backend resolves the mapping key and treats continued array traversal as a nonmatch

#### Scenario: Filter cannot bypass excluded scope
- **WHEN** an expression names metadata present only in an excluded subtree
- **THEN** no excluded identity becomes eligible or visible through hits, counts, explanations, or errors
