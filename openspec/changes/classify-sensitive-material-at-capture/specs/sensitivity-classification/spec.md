## ADDED Requirements

### Requirement: Sensitivity classes are registry data

The `sensitivity-classes` vocabulary registry SHALL hold every sensitivity class: a shipped pack and a vault overlay.
- Each entry SHALL declare `default_disclosure`, a closed attribute whose values are `owner-only` and `vault-default`.
- The shipped pack SHALL declare `identity`, `health` and `financial` with `default_disclosure: owner-only`.
- Promotion SHALL be `propose-save`.
- Code SHALL branch only on `default_disclosure`. Code SHALL NOT hold a list of class keys or branch on one.

#### Scenario: An overlay adds a class

- **WHEN** the owner saves a `sensitivity-classes` entry `legal` with `default_disclosure: owner-only` through `schema_memory`
- **THEN** a capture may declare `sensitivity: [legal]`, and its result reports the class

#### Scenario: A class without its attribute

- **WHEN** an overlay entry omits `default_disclosure` or gives another value
- **THEN** the registry reports a finding, and the entry is not a class a page may carry

### Requirement: A page's class is metadata that never rewrites captured bytes

A page SHALL carry its classes in the `sensitivity` frontmatter field, as a list of registered class keys. On a page in an append-only tree, only the classify operation SHALL change the field. It goes through the governed pointer write:
- every byte outside the field's value is identical before and after the write;
- the file keeps its path;
- one reason record allows a revert.

A page without the field SHALL read as unclassified.

#### Scenario: Classifying a captured Source

- **WHEN** the owner classifies a Source as `identity` with a reason
- **THEN** the Source's body bytes and path are unchanged, and its frontmatter differs only in `sensitivity`
- **AND** a revert restores the exact previous bytes

#### Scenario: An unknown class

- **WHEN** a capture or classify call names a class the registry does not hold
- **THEN** it refuses with `SENSITIVITY_CLASS_UNKNOWN`, names the registered keys, and writes nothing

#### Scenario: A plain edit cannot change the class of a Source

- **WHEN** an edit, a correction or a reclassify of a Source changes its `sensitivity` value
- **THEN** the write refuses with `APPEND_ONLY` and the Source is unchanged

### Requirement: Capture records the agent's classification

`save_source`, `save_evidence`, `create_note`, `save_episode` and `add_record` SHALL accept an optional `sensitivity` list, which the calling agent decides. When a Source or Evidence capture gives none, the capture SHALL apply its Source kind's `sensitivity` attribute when the kind declares one. The server SHALL NOT infer a class from content by any word list, pattern or model call.

#### Scenario: An agent captures an identity document photo

- **WHEN** an agent saves a photo of a synthetic identity card as Evidence with `sensitivity: [identity]`
- **THEN** the full image bytes are kept, the Evidence page carries `sensitivity: [identity]`
- **AND** the result reports `basis: declared` and the disclosure in force

#### Scenario: A kind default applies

- **WHEN** the vault overlay gives a Source kind `sensitivity: identity` and a capture of that kind declares no class
- **THEN** the Source carries `sensitivity: [identity]` and the result reports `basis: kind-default`

### Requirement: Results report the class and the disclosure in force

Each capture and classify result SHALL report `sensitivity` with:
- `classes`
- `basis`: `declared`, `kind-default` or `none`
- `disclosure`, taken from live governance state

It SHALL NOT report a restriction that nothing enforces.

#### Scenario: No governance policy

- **WHEN** a vault has no governance policy and an agent captures a page with `sensitivity: [health]`
- **THEN** `disclosure` says that governance is off and every client signed in as the owner reads the page

### Requirement: Collections carry a class floor

A collection definition MAY declare `sensitivity`, and every row SHALL take it. A row MAY add classes. A row SHALL NOT remove a class that its collection declares. A row's classes SHALL be stored with its governance metadata.

#### Scenario: A health collection

- **WHEN** a collection declares `sensitivity: [health]` and an agent adds a row without a class
- **THEN** the row's governance metadata carries `health`

### Requirement: Agents keep sensitive material and never protect it by omission

The shipped instructions and workflow skills SHALL tell agents to:
- keep identity documents, government ID numbers, health records and financial statements in full;
- classify them;
- never skip, cover or reduce one to a pointer to protect it;
- ask the owner only when a disclosure decision is genuinely unclear.

They SHALL keep the exclusion of authenticators: a payment card number, a security code, a password or a live credential.

#### Scenario: An owner hands an agent their ID-card photos

- **WHEN** an owner asks an agent to keep photos of their identity card
- **THEN** the agent saves the full images in that turn, classifies them `identity`, and says so in one line, without asking for a covered copy
