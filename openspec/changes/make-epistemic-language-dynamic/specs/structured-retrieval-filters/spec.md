## ADDED Requirements

### Requirement: Units Filter By Epistemic Dimension
The structured-filter field registry SHALL accept `unit.dimension` as a closed field that takes a dimension key or a label in any language, with `$eq` for belonging to a dimension, `$in` for belonging to one of several, and `$ne` for not belonging to one. It SHALL reject every other operator and every operand that is not a string. A name that no dimension entry holds SHALL be rejected with the keys that exist. Membership SHALL come from the one membership rule of the dimension registry, so a dimension that a vault adds is filterable with no code change. A unit SHALL be an end of each edge that its own relation row starts and of each edge whose anchored link ends at it. An edge that starts or ends at a whole page, such as one written in frontmatter, in a page's relations section or as a link with no anchor, SHALL make every unit of that page an end. A dimension whose membership depends on typed edges SHALL read those edges in one graph read for the units it tests, SHALL admit an edge only when the caller may read both of its pages, and SHALL raise the typed warming outcome while the graph is missing or stale, never an empty answer. A served reader SHALL answer the field without a corpus scan. For a dimension without family members, the index SHALL propose occurrences from member kinds and authored governed-key presence. Selected interpretation SHALL decide actual carried-key membership against the original filter before each lane cap and the final limit. This SHALL hold under Boolean composition, including `$or`, `$not` and `$ne`, and SHALL fill the limit when enough eligible matches exist within the per-pass bound on exact checks defined below. Authored presence SHALL never substitute for a parsed value or become an unsafe complement. Registry edits SHALL require no corpus rebuild. The lexical owner SHALL derive, replace, delete and verify the same neutral row-key projection in file mode and store mode. The file-mode reader SHALL check dimensions with family members on its hits, with one graph read for the hit set. It SHALL accept such a predicate only as a top-level condition of the filter, alone or under a top-level `$and`, and SHALL refuse it under `$or` or `$not` with `UNSUPPORTED_FILTER_FIELD` and a message that says to move it to the top level. Because the limit applies before the check, the result's leading outcome SHALL say when the check dropped hits from a full ranking, and that more units in the dimension may exist.

Canonical-store catalogue plans SHALL propose family candidates through indexed neutral endpoints and authored target dependencies.
They SHALL decide exact membership before every lexical or vector candidate cap and the final limit, including Boolean composition.
The author's admitted registry SHALL determine terminal replacement, family and display behaviour before folding unit or page endpoints.
The original filter SHALL remain authoritative, and the same eligible identities SHALL reach both lanes.
Selected target resolution SHALL use complete zero/one-owner proofs or two admitted ambiguity witnesses, without claiming an exact ambiguity count.
Incomplete proofs or incomplete required owner projections SHALL return the existing typed warming outcome, including under negation.
Proposals consuming no family inputs SHALL NOT wait for that projection.
The normalizer keys and ordinary-body witnesses SHALL remain neutral, maintained by the common lexical and graph owners.
Registry changes SHALL require no corpus rebuild, and an endpoint outside the result's page filter SHALL still count when readable.

A ranked lexical scan into which a query with content pushes a dimension predicate, whether its members are kinds, keys or families, in file mode or store mode, SHALL check each proposed row exactly within a per-pass work bound.
The bound SHALL be eight times the pass's candidate window. The window derives from the requested limit, and it doubles each time the search widens, so each widened pass gets a doubled bound.
Two exact-check paths remain unbounded: a dimension predicate joined under `$and` to a `unit.kind` or `unit.category` seed, which reads every eligible seeded row, and a request without query text, which widens its prefix until it fills the limit.
The bound SHALL count the proposed rows that fail the filter, and the scan SHALL stop when that count reaches it.
For a restricted caller, whose lexical lane ranks by admitted statistics, a unit with several eligible aliases SHALL be checked before scoring, outside the bound.
The vector lane's read of a dimension's newest members SHALL stop at the same bound.
A stop SHALL mark the result as partial coverage, never as a failed search method. The stop SHALL NOT keep the result out of the result cache, a cached result SHALL keep its marker, and the stop SHALL NOT count as a degradation.
A result marked as partial coverage SHALL carry the notice that the dimension filter ran on the best matches only, so more matches in it may exist.
A keyword result, which keeps the scan's recency order, SHALL carry that notice when its scan stopped and it holds fewer units than the limit, not counting units of superseded pages when the request prefers active ones.
A hybrid result SHALL carry it when its lexical scan stopped, or when its vector lane ranked the newest members and that read stopped.
A vector result SHALL carry it when its vector lane ranked the newest members and that read stopped, or when it fell back to a lexical scan that stopped.

#### Scenario: Stored family members fill the requested limit before ranking
- **WHEN** fewer better-ranked nonmembers than the bound on exact checks precede enough admitted family members in a canonical-store catalogue request
- **THEN** keyword, hybrid and vector results fill the requested limit with exact members
- **AND** nested `$or`, `$not` and `$ne` keep the original predicate before candidate caps

#### Scenario: Author-specific replacement meaning survives a query instance
- **WHEN** two admitted instances define the same extension key under different core parents
- **THEN** stored family filtering and display marks use each author's terminal replacement meaning
- **AND** explicit core-qualified relations and selected fragments retain their unit or page grain

#### Scenario: A withheld title competitor reveals an incoming edge
- **WHEN** withholding one title competitor leaves one ordinary admitted target for an authored incoming relation
- **THEN** indexed dependency lookup discovers that relation and resolves it from stored selected structures
- **AND** family preparation parses no unreturned author page and acquires no complete resolver namespace
- **AND** a retired collection alias supplies no ordinary existing endpoint

#### Scenario: A parser upgrade never becomes an exact empty answer
- **WHEN** stored semantic rows reflect another parser version and a filtered request depends on those rows
- **THEN** the request returns the existing typed warming outcome before candidate SQL or a cached miss
- **AND** the existing bounded job restarts older generation cursors and qualifies the parser only after its final full batch
- **AND** raw pages, path lists and complete inbound links remain available while semantic counts remain unknown

#### Scenario: Filtering by a dimension a vault added
- **WHEN** a vault dimension `regulation` has the kinds `constraint` and `requirement` as members, and a request filters units by `{"unit.dimension": {"$eq": "regulation"}}`
- **THEN** the request returns the units of those kinds and no others, with no code change

#### Scenario: A label resolves to its dimension
- **WHEN** a request filters by the label `Regulation` of that dimension
- **THEN** it returns the same units as the key does

#### Scenario: An unknown dimension is refused
- **WHEN** a request filters by a name that no dimension entry holds
- **THEN** the request fails with an invalid-value error that lists the existing dimension keys

#### Scenario: A missing file-mode graph never reads as no edges
- **WHEN** a file-mode request filters by a dimension that has a family member, and the graph sidecar is missing
- **THEN** the request raises the typed warming outcome and returns no hits as an answer

#### Scenario: An edge counts at the unit that carries it
- **WHEN** a page holds three claims and only the first carries a `supersedes` relation row, and a request filters units by `{"unit.dimension": {"$eq": "currency"}}`
- **THEN** the first claim is returned and the other two are not, while every unit of the superseded page is returned, because the row's link names that page with no anchor

#### Scenario: An edge between two units of one page counts
- **WHEN** a decision on a page carries `supersedes: [[#before]]`, which names an earlier decision on the same page
- **THEN** a request filtering by the same dimension returns both decisions

#### Scenario: A served reader rewrites an indexed dimension
- **WHEN** a served request filters by a dimension that has no family member, under `$or` or `$not` or at the top level
- **THEN** the request returns the units the offline reader returns for the same filter, and no corpus scan runs

#### Scenario: Governed-key members fill the requested limit
- **WHEN** fewer better-ranked nonmembers than the bound on exact checks precede enough units with valid member rows, including rows carried by an admitted vault kind
- **THEN** keyword, hybrid and vector retrieval return the requested number of eligible units without a corpus scan
- **AND** authored rows with invalid values consume no lane slot

#### Scenario: A broad dimension ranks its newest members within a bound
- **WHEN** a hybrid or vector request filters by a dimension with more members than the bound, and other units of their pages outrank every member
- **THEN** the vector lane ranks the newest members within that bound and the result fills the requested limit
- **AND** the result carries the partial-coverage notice and no failed search method

#### Scenario: A sparse family bounds its exact checks
- **WHEN** each page of a canonical-store vault holds eight units with the query word and one family member without it, and a request filters by that family's dimension
- **AND** the bound is set to one candidate window, so both corpus sizes exceed it
- **THEN** the request makes the same number of exact checks on 8 pages and on 24 pages, for an owner and for a restricted caller
- **AND** the hybrid result fills its limit with members, and the keyword result is empty, because no member holds the query word
- **AND** both results carry the partial-coverage notice and no failed search method
- **AND** an owner's repeat of a request that asks for no dimension groups is served from the cache with that notice

#### Scenario: A complement keeps an invalid authored row
- **WHEN** a served filter negates a governed-key dimension and an authored member row has no valid selected value
- **THEN** the unit remains eligible through `$not` and `$ne`, as the offline reader decides

#### Scenario: A registry edit changes membership without rebuilding the corpus
- **WHEN** a dimension changes its kinds or keys while the catalogue remains unchanged
- **THEN** the next served filter uses the new membership

#### Scenario: An owner upgrade restarts an older cursor
- **WHEN** a stored rederivation cursor belongs to an older owner generation or records no generation
- **THEN** the new owner pass derives every page from the start before it stamps its generation current
- **AND** an interruption within the same generation resumes after its committed cursor

#### Scenario: Partial row-key upgrades cannot answer exact filters
- **WHEN** a compiled proposal consumes row-key presence and the current owner generation has not finished deriving it
- **THEN** the request returns the existing typed retrieval warming outcome, including for negative and Boolean filters
- **AND** unrelated recall and proposals simplified to kinds alone remain available
- **AND** inbound links already complete for their introduction generation remain available during a later row-key upgrade
- **AND** after the final owner stamp the dependent filter answers exactly

#### Scenario: A file-mode reader checks an edge dimension on its hits
- **WHEN** a served file-mode request filters by a dimension that has a family member, at the top level of the filter
- **THEN** the request returns only the ranked units that belong to the dimension, with one graph read for the hits and no corpus scan
- **AND** when the check dropped units from a full ranking, the reply's leading outcome says that the dimension filter ran only on the best matches, how many of them it kept, and that more may exist, and names no count of the matches the server checked

#### Scenario: A file-mode reader refuses an edge dimension under a logical operator
- **WHEN** a served file-mode request names a dimension that has a family member under `$or` or `$not`
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

### Requirement: Invalid Filters Fail Closed And Identically
The filter compiler SHALL validate namespace, RFC 6901 pointer, reserved field, operator, value arity, and resolved value type before candidate generation. Errors SHALL identify the JSON path, stable code, expected shape/type, and remediation. Missing frontmatter values SHALL differ from explicit null and SHALL be testable through `$exists`. Reserved page/unit fields SHALL reuse existing shortcut canonicalization/case semantics; arbitrary frontmatter strings SHALL compare exactly after YAML parsing. Access scope and excluded-subtree policy SHALL apply before caller filters and MUST NOT be weakened by them. Every retrieval backend and facade SHALL consume the same normalized typed filter AST; an unsupported backend capability MUST fail or use a proven equivalent post-filter before ranking and MUST NOT silently broaden results. The file-mode exception is a `unit.dimension` condition with a family member: that reader SHALL check it on the ranked hits after the limit, the result SHALL say that the check ran only on the best matches and that more may exist, and no unit outside the dimension SHALL be returned. Canonical-store catalogue plans SHALL evaluate family membership before ranking and retain the original Boolean expression; this file-mode exception SHALL NOT apply to them.

#### Scenario: Misspelled field does not look like an empty result
- **WHEN** a caller uses the unknown closed unit field `unit.categry`
- **THEN** recall returns an invalid-filter-field error pointing to that path rather than zero hits

#### Scenario: Missing and null remain distinct
- **WHEN** one page omits `owner` and another declares `owner: null`
- **THEN** `$exists:false` matches only the missing field while `$eq:null` matches only the explicit null

#### Scenario: Backend parity is enforced
- **WHEN** the same structured filter runs through lexical, vector, hybrid, graph-enriched, SQLite, and any optional search backend
- **THEN** every lane starts from the same eligible identity set and no backend returns a filtered-out candidate
- **AND** when a served reader checks a `unit.dimension` condition on its ranked hits, every lane starts from the eligible set without that condition, the check applies to every lane's hits alike, and the result says that it ran only on the best matches

#### Scenario: Heterogeneous scalar and array fields stay backend-identical
- **WHEN** equivalent `$eq`, `$in`, `$contains`, and `$all` predicates run over a field that is scalar, array, null, missing, or a mapping on different pages
- **THEN** every backend applies the same exact-type, terminal-array, and nonmatch rules

#### Scenario: Numeric mapping key is not mistaken for array traversal
- **WHEN** the same RFC 6901 pointer segment `0` follows a mapping in one candidate and an array in another
- **THEN** every backend resolves the mapping key and treats continued array traversal as a nonmatch

#### Scenario: Filter cannot bypass excluded scope
- **WHEN** an expression names metadata present only in an excluded subtree
- **THEN** no excluded identity becomes eligible or visible through hits, counts, explanations, or errors
