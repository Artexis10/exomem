## ADDED Requirements

### Requirement: A scope may select by sensitivity class

A scope and its `exclude` SHALL accept a `sensitivity` selector: a list of registered `sensitivity-classes` keys. It SHALL match a page or row whose effective classes intersect the list. An unregistered key SHALL be a compile error. The selector set SHALL stay closed, so a binary without this selector refuses such a policy.

#### Scenario: An owner-only class

- **WHEN** a policy has a scope `{sensitivity: [health], default_deny: true}` and no rule grants it to an audience
- **THEN** a non-owner audience gets every page with effective class `health` as missing, and the owner reads them

#### Scenario: A cited page under a class scope

- **WHEN** a note cites a `health` Source and a scope selects `sensitivity: [health]`
- **THEN** the scope selects the note as well

### Requirement: A class-selecting policy needs the vault store

A vault that does not run the vault store SHALL refuse to compile a policy with a scope or exclusion that selects by `sensitivity`. The error SHALL name the vault store as the requirement.

#### Scenario: A file-mode vault

- **WHEN** an owner writes a `sensitivity` scope on a vault in file mode
- **THEN** the compile refuses, the previous policy stays in force, and the error names the vault store

### Requirement: Path selectors ignore letter case

Path selectors and path exclusions SHALL match after casefolding both the pattern and the path.

#### Scenario: A folder spelled in another case

- **WHEN** a scope selects `paths: [Health/**]` and a page lives at `health/visit.md`
- **THEN** the scope selects the page

### Requirement: A create refusal does not reveal an unreadable page

A create whose target path holds a page the caller cannot read SHALL refuse with the answer that the caller gets for a path outside its writable scopes. It SHALL NOT answer `FILE_EXISTS`.

#### Scenario: A confined writer creates over a withheld page

- **WHEN** a confined writer creates a note at the path of a page withheld from it
- **THEN** the refusal matches the refusal for an unwritable path, in shape and in timing

### Requirement: Every non-owner release door decides through egress

Every code path that releases page, row or binary content, or a fact derived from it, to a caller other than the owner SHALL release only what the egress decision allows. Every call site of the store snapshot reader SHALL declare its reader as owner-only or egress-decided, with a reason. A test SHALL fail when a call site declares neither. A binary SHALL be decided by its Evidence page's effective classes.

#### Scenario: A new door reads the snapshot

- **WHEN** a change adds a call to the store snapshot reader without a reader declaration
- **THEN** the test suite fails and names the call site
