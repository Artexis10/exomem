## ADDED Requirements

### Requirement: A scope may select by sensitivity class

A scope SHALL accept a `sensitivity` selector: a list of registered `sensitivity-classes` keys. It SHALL match a page or row whose effective classes intersect the list. A scope that uses it SHALL have no other selector, and `exclude` SHALL NOT accept it. An unregistered key SHALL be a compile error. The selector set SHALL stay closed, so a binary without this selector refuses such a policy.

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

The result postfilter that every surface's results pass SHALL decide membership on effective classes. A fact released without a path, such as a count, a rollup, a snippet, a title or a suggestion, SHALL be decided for the pages it derives from. A binary SHALL be decided by its Evidence page's effective classes. A test SHALL run every registered read operation, MCP resource and prompt under a restricted caller over synthetic classified pages. It SHALL fail when any of their paths, titles or text appears in a result, or when a count differs from the same vault without them.

#### Scenario: A new read operation

- **WHEN** a change registers a read operation that returns a classified page's title to a restricted caller
- **THEN** the test suite fails and names the operation

### Requirement: A class scope never takes a page out of the residual scope

When residual membership is assigned, a match by a scope whose selector is `sensitivity` SHALL NOT count as another scope selecting the item. Such a scope SHALL only ever lower an item's ceiling. An item's effective sensitivity classes SHALL count as part of the item for the rule that membership is a function of the item, its path and the compiled policy.

#### Scenario: A class scope keeps the residual restriction

- **WHEN** a residual scope declares `default_deny`, a scope selects `sensitivity: [health]`, a rule grants the health scope to an audience, and a page with effective class `health` matches no other scope
- **THEN** the page keeps the residual scope as well, and that audience still gets it as missing
