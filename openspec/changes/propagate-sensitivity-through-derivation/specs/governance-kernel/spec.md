## ADDED Requirements

### Requirement: A scope may select by sensitivity class

A scope SHALL accept a `sensitivity` selector of registered class keys. It SHALL match pages and rows with any listed effective class. A match SHALL only lower the ceiling. This scope SHALL have no other selector, and `exclude` SHALL NOT accept it. An unregistered key or an unknown selector SHALL be a compile error.

#### Scenario: An owner-only class

- **WHEN** a policy has a scope `{sensitivity: [health], default_deny: true}` and no rule grants it to an audience
- **THEN** a non-owner audience gets every page with effective class `health` as missing, and the owner reads them

#### Scenario: A cited page under a class scope

- **WHEN** a note cites a `health` Source and a scope selects `sensitivity: [health]`
- **THEN** the scope selects the note as well

#### Scenario: An exclusion by class

- **WHEN** a policy's scope declares `exclude: {sensitivity: [health]}`
- **THEN** the compile refuses and names the rule

### Requirement: A class-selecting policy needs the vault store

A vault that does not run the vault store SHALL refuse to compile a policy with a scope that selects by `sensitivity`. The error SHALL name the vault store as the requirement.

#### Scenario: A file-mode vault

- **WHEN** an owner writes a `sensitivity` scope on a vault in file mode
- **THEN** the compile refuses, the previous policy stays in force, and the error names the vault store

### Requirement: Path selectors ignore letter case

Path selectors and path exclusions SHALL match after casefolding both the pattern and the path.

#### Scenario: A folder spelled in another case

- **WHEN** a scope selects `paths: [Health/**]` and a page lives at `health/visit.md`
- **THEN** the scope selects the page

### Requirement: The egress decision reads effective classes

The result postfilter that every surface's results pass SHALL decide membership on effective classes. A fact released without a path, such as a count, a rollup, a snippet, a title or a suggestion, SHALL be decided for the pages it derives from. A binary SHALL be decided by its Evidence page's effective classes.

#### Scenario: A binary whose Evidence page inherits a class

- **WHEN** a scan's Evidence page inherits `identity` and a restricted audience requests the scan
- **THEN** the download answers as missing

### Requirement: A behavioural test covers every read door

A test SHALL run every registered read operation, MCP resource and prompt under a restricted caller over synthetic classified pages. It SHALL fail when any of their paths, titles or text appears in a result, or when a count differs from the same vault without them.

#### Scenario: A new read operation

- **WHEN** a change registers a read operation that returns a classified page's title to a restricted caller
- **THEN** the test suite fails and names the operation

### Requirement: A residual scope uses ordinary policy rules

A residual scope SHALL be an ordinary scope that rules, grants, release grants, `default_deny`, organisation caps, connector denied sets and private vocabulary instances name by id. When the connector boundary protects it, its pinned document SHALL include `residual`.

#### Scenario: A boundary pins the residual scope
- **WHEN** a connector boundary protects the residual scope
- **THEN** its pinned scope document includes `residual`

### Requirement: The residual scope has no selectors or exclusions

A policy MAY declare at most one residual scope with `residual: true`. The compile SHALL refuse a residual scope with a selector or exclusion, a second residual scope or a non-boolean `residual`.

#### Scenario: A residual scope declares a selector
- **WHEN** a scope sets `residual: true` and supplies `paths`
- **THEN** policy compilation refuses the scope

## MODIFIED Requirements

### Requirement: A residual scope selects what no other scope selects

Membership SHALL assign the residual scope to a page or file exactly when no registry binding or non-`sensitivity` scope selects it. Membership SHALL use the item, including effective classes, its path and the compiled policy. Public registry storage counts as classified with no scope. The residual scope SHALL never select a directory. Without a residual scope, membership SHALL stay as before.

#### Scenario: An unclassified page is withheld from an unnamed audience
- **WHEN** a residual scope declares `default_deny` and an audience that no rule names reads a page that no other scope selects
- **THEN** the page is answered as a missing page, and the owner receives it in full

#### Scenario: Classification moves a page out of the residual scope
- **WHEN** the owner adds a scope with a selector other than `sensitivity` that selects a residual page
- **THEN** the next decision for that page uses the new scope and not the residual scope

#### Scenario: A fenced connector and an opted-in connector
- **WHEN** one client's denied set lists the residual scope and another client's does not
- **THEN** the first receives no unclassified page from any read, search, recall or recent-context door, and the second receives them

#### Scenario: Vocabulary and folders stay available
- **WHEN** a fenced connector interprets pages or browses a folder that holds pages it may see
- **THEN** public vocabulary overlays and history are admitted, and the folder is listed

#### Scenario: A vault without a residual scope
- **WHEN** a policy declares no residual scope
- **THEN** scope membership is unchanged by the exception

#### Scenario: A class scope keeps the residual restriction

- **WHEN** a residual scope declares `default_deny`, a scope selects `sensitivity: [health]`, a rule grants the health scope to an audience, and an otherwise unclassified page matches only that class scope
- **THEN** the page keeps the residual scope as well, and that audience still gets it as missing
