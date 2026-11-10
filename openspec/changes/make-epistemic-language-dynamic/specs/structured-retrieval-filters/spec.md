## ADDED Requirements

### Requirement: Units Filter By Epistemic Dimension
The structured-filter field registry SHALL accept `unit.dimension` as a closed field that takes a dimension key or a label in any language, with `$eq` for belonging to a dimension, `$in` for belonging to one of several, and `$ne` for not belonging to one. It SHALL reject every other operator and every operand that is not a string. A name that no dimension entry holds SHALL be rejected with the keys that exist. Membership SHALL come from the one membership rule of the dimension registry, so a dimension that a vault adds is filterable with no code change. A unit SHALL be an end of each edge that its own relation row starts and of each edge whose anchored link ends at it. An edge that starts or ends at a whole page, such as one written in frontmatter, in a page's relations section or as a link with no anchor, SHALL make every unit of that page an end. A dimension whose membership depends on typed edges SHALL read those edges in one graph read for the units it tests, SHALL admit an edge only when the caller may read both of its pages, and SHALL raise the typed warming outcome while the graph is missing or stale, never an empty answer. A served reader SHALL answer the field without a corpus scan. It SHALL rewrite a dimension that has no family member, and whose member keys the index can each answer, into the equivalent indexed predicates, anywhere in the filter. It SHALL check every other dimension on its hits, with one graph read for the hit set. It SHALL accept such a predicate only as a top-level condition of the filter, alone or under a top-level `$and`, and SHALL refuse it under `$or` or `$not` with `UNSUPPORTED_FILTER_FIELD` and a message that says to move it to the top level. Because the limit applies before the check, the result's leading outcome SHALL say when the check dropped hits from a full ranking, and that more units in the dimension may exist.

#### Scenario: Filtering by a dimension a vault added
- **WHEN** a vault dimension `regulation` has the kinds `constraint` and `requirement` as members, and a request filters units by `{"unit.dimension": {"$eq": "regulation"}}`
- **THEN** the request returns the units of those kinds and no others, with no code change

#### Scenario: A label resolves to its dimension
- **WHEN** a request filters by the label `Regulation` of that dimension
- **THEN** it returns the same units as the key does

#### Scenario: An unknown dimension is refused
- **WHEN** a request filters by a name that no dimension entry holds
- **THEN** the request fails with an invalid-value error that lists the existing dimension keys

#### Scenario: A missing graph never reads as no edges
- **WHEN** a request filters by a dimension that has a family member, and the graph sidecar is missing
- **THEN** the request raises the typed warming outcome and returns no hits as an answer

#### Scenario: An edge counts at the unit that carries it
- **WHEN** a page holds three claims and only the first carries a `supersedes` relation row, and a request filters units by `{"unit.dimension": {"$eq": "currency"}}`
- **THEN** the first claim is returned and the other two are not, while every unit of the superseded page is returned, because the row's link names that page with no anchor

#### Scenario: An edge between two units of one page counts
- **WHEN** a decision on a page carries `supersedes: [[#before]]`, which names an earlier decision on the same page
- **THEN** a request filtering by the same dimension returns both decisions

#### Scenario: A served reader rewrites an indexed dimension
- **WHEN** a served request filters by a dimension that has no family member and whose member keys the index answers, under `$or` or `$not` or at the top level
- **THEN** the request returns the units the offline reader returns for the same filter, and no corpus scan runs

#### Scenario: A served reader checks an edge dimension on its hits
- **WHEN** a served request filters by a dimension that has a family member, at the top level of the filter
- **THEN** the request returns only the ranked units that belong to the dimension, with one graph read for the hits and no corpus scan
- **AND** when the check dropped units from a full ranking, the reply's leading outcome says that the dimension filter ran only on the best matches, how many of them it kept, and that more may exist, and names no count of the matches the server checked

#### Scenario: A served reader refuses an edge dimension under a logical operator
- **WHEN** a served request names a dimension that has a family member under `$or` or `$not`
- **THEN** the request is refused with `UNSUPPORTED_FILTER_FIELD`, its message says to move the predicate to the top level of the filter, and no corpus scan runs

## MODIFIED Requirements

### Requirement: Governed Unit Metadata Is Filterable
The structured-filter field registry SHALL accept `unit.verdict` as a closed string field and `unit.check_by` as a closed typed-date field. Both SHALL be evaluated against the same parsed semantic unit as the other `unit.*` fields, and a filter that names a `unit.*` field that the namespaced filter contract does not list SHALL still be rejected as unknown. `unit.verdict` operands SHALL be canonicalized by trimming and casefolding, and SHALL be rejected when they are not strings for the exact and collection operators.

#### Scenario: A verdict predicate compiles and matches
- **WHEN** a filter expression contains `{"unit.verdict": {"$eq": "Refuted"}}`
- **THEN** the plan compiles, carries a unit predicate, and matches a unit whose verdict is `refuted`

#### Scenario: A verdict predicate excludes other verdicts
- **WHEN** the same expression is evaluated against a unit whose verdict is `confirmed`
- **THEN** the unit does not match

#### Scenario: An absent verdict is distinguishable
- **WHEN** a filter expression contains `{"unit.verdict": {"$exists": false}}`
- **THEN** a unit carrying no verdict metadata row matches and a unit carrying one does not

#### Scenario: An unknown unit field stays rejected
- **WHEN** a filter expression names `unit.confidence`
- **THEN** compilation raises an unknown-filter-field error

### Requirement: Namespaced Page And Unit Filter Contract
Recall SHALL accept a `filters` JSON object over explicit page and unit namespaces. Reserved page system fields SHALL use fixed names such as `page.status`, `page.project`, `page.updated`, and `page.file_type`. Arbitrary frontmatter SHALL use `page.frontmatter:/<RFC-6901-pointer>` with standard `~0`/`~1` escaping. Pointers SHALL traverse runtime mappings only; arrays SHALL be terminal scalar collections, and encountering an array before exhausting the pointer SHALL make that candidate a nonmatch. Numeric pointer segments SHALL remain valid mapping keys when the runtime value is a mapping. Mappings MAY be traversed or tested for existence but MUST NOT be equality-compared. `unit.<field>` SHALL be restricted to `category`, `category_key`, `kind`, `tags`, `context`, `form`, `verdict`, `check_by`, and `dimension`. Unit results SHALL evaluate the complete expression against one parent/unit pair. Page results SHALL evaluate it existentially over one child unit at a time, with a missing-unit sentinel for page-only branches, so separate units cannot jointly satisfy one expression. A page-only expression SHALL preserve page-level `auto` behavior; any `unit.*` predicate SHALL make `result_level="auto"` resolve to unit level.

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
