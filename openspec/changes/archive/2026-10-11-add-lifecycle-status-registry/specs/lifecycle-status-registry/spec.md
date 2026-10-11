## Purpose

Let a vault define page status labels through governed vocabulary while preserving stable public meanings, truthful classification and reversible changes.

## ADDED Requirements

### Requirement: Page status definitions use the governed registry contract

The status registry SHALL map labels and aliases to `live`, `pending`, `superseded`, `retired`, or `abandoned`.
It SHALL expose the existing inspect, propose, save, history and restore operations through `schema_memory(subject="statuses")`.
Its mutation authority, meaning continuity and history SHALL follow the shared vocabulary contract.
Registry operations SHALL preserve authored page bytes.

#### Scenario: A new abandoned label changes current service and can be restored
- **WHEN** an owner saves a previously unregistered page label with class `abandoned`, then restores the preceding registry version
- **THEN** the next activation after save stops serving that page as current
- **AND** the next activation after restore treats the label as unregistered and live, with debt
- **AND** neither registry operation changes the page bytes

### Requirement: Shipped canonical meanings remain independently usable

A normalized canonical label from the shipped pack SHALL always retain its shipped class.
An overlay SHALL NOT shadow, remove, redirect, deprecate or change that canonical meaning.
Extension keys, labels and aliases SHALL NOT collide with canonical pack keys.
Known canonical classification SHALL depend only on the public pack, including when an overlay is denied or invalid.
Absent or empty page status SHALL retain the public live default, require no overlay admission, and create no unregistered debt.
Malformed non-string values SHALL retain public-live selection without debt or overlay admission.
The raw value SHALL remain unchanged for existing structural validation of malformed frontmatter.
This parser fallback applies where raw frontmatter remains available; existing text-only metadata retains its documented loss of the original scalar type.
No spelling heuristic SHALL infer a lost type from an indexed status string.
Inspection and extension resolution SHALL report an admitted invalid overlay honestly.

#### Scenario: Private extensions do not disable ordinary canonical behavior
- **WHEN** the same admitted page uses a canonical label across absent, withheld-valid and withheld-invalid overlays
- **THEN** its classification and dependent public results remain equivalent
- **AND** no private hash, alias, debt or existence-dependent warning appears

#### Scenario: Statusless pages remain live without private configuration
- **WHEN** an admitted statusless page is used across admitted and withheld status configuration
- **THEN** its live classification and dependent public results remain equivalent
- **AND** no registry admission is needed and no unregistered-status debt appears

#### Scenario: Every write and parse preserves canonical meanings
- **WHEN** save, restore or a direct edit attempts to redirect a canonical label or change its class
- **THEN** admitted registry validation rejects that definition
- **AND** canonical-only classification still retains the shipped meaning

### Requirement: Private extension classification requires admitted definitions

The system SHALL admit extension definitions before reading their entries, aliases or identity.
A denied unfamiliar label SHALL produce opaque classification unavailability, never a pack fallback or unregistered claim.
Denied absent and denied present overlays SHALL be indistinguishable.
Only behavior that requires that classification SHALL become unavailable; independently admitted page reads SHALL remain usable.

#### Scenario: A private alias is indistinguishable from denied absent configuration
- **WHEN** a restricted caller sees identical pages and a denied registry path with either no overlay, a private key or a private alias
- **THEN** extension-dependent classification reports equivalent unavailability
- **AND** no key-specific debt, private digest or existence-dependent result appears
- **AND** an independently admitted read still returns the page

#### Scenario: An admitted absent overlay is a real pack-only registry
- **WHEN** the registry path is admitted and the overlay is absent
- **THEN** canonical labels use the pack and an unfamiliar label remains live with visible unregistered debt

### Requirement: Consumers apply purpose-specific class predicates

Activation and minimum-unit applicability SHALL require class `live`.
Named carry SHALL permit `live` and `pending`, retaining its separate successor exclusion.
Historical currency SHALL recognize `superseded` and `retired`; the existing ranking penalty SHALL apply to `superseded`.
Recurrence evidence SHALL exclude `superseded`, `retired` and `pending`.
Authored same-page supersession SHALL remain effective independently of registry changes.

#### Scenario: Pending carry remains addressable without becoming current activation
- **WHEN** a turn explicitly names an admitted pending page
- **THEN** named carry remains possible and preserves its pending classification
- **AND** the page does not qualify as an active compiled result

#### Scenario: Planned evidence no longer supplies recurrence spread
- **WHEN** an otherwise eligible planned page supplies the final page needed for a recurrence finding
- **THEN** its pending class prevents that spread contribution and anchor selection
- **AND** planned entity resolution remains unchanged because existing entity-tree and identity rules still apply

#### Scenario: Abandoned evidence preserves the existing recurrence boundary
- **WHEN** otherwise eligible evidence has class `abandoned`
- **THEN** recurrence may still count its authored links, as it counted the shipped dropped status
- **AND** activation and named current carry do not serve it as current

#### Scenario: Registry restore does not erase authored supersession
- **WHEN** registry restore changes page classification but an admitted same-page relation supersedes one of its units
- **THEN** the unit remains superseded under the existing relation rule

### Requirement: Committed changes reach the next operation without a restart

A committed status save or restore SHALL affect the next operation without changing page bytes or restarting the service.
Each operation SHALL use one consistent classification basis.
Reusable results SHALL bind their actual public-pack or admitted-effective-registry dependency and caller admission.
Cached owner state SHALL NOT disclose a private registry identity to another audience.

#### Scenario: Warm find and activation observe save and restore
- **WHEN** find and activation are warm before a status save and its later restore
- **THEN** each subsequent operation observes the corresponding classification
- **AND** previously cached current or historical results cannot survive under the wrong registry identity

### Requirement: Authoring guidance separates normative rules from live vocabulary

The semantic contract SHALL teach the fixed class rule with a new version and digest.
Admitted status definitions and their identity SHALL remain separate from its normative fields.
Clients SHALL discover current status vocabulary through the exposed registry contract.
Released hosted profiles SHALL retain their historical semantic contract artifacts and descriptors while using current shared runtime semantics.
The versioned bootstrap operating contract SHALL provide separate corrective guidance for the current class rule and classification outcomes.
Compact and full bootstrap SHALL expose that correction without requiring a parameter absent from the frozen descriptor.
Registry lookup guidance SHALL name only routes exposed by the active profile.
Unknown labels SHALL create debt only after admitted classification establishes that they are unregistered.

#### Scenario: Frozen clients and current clients receive their own contracts
- **WHEN** a current client and a released hosted profile request their authoring contract
- **THEN** the current client receives the class rule and current registry route
- **AND** the released profile retains its published contract
- **AND** its bootstrap operating contract explains the current shared runtime behavior separately

#### Scenario: A frozen client learns the changed validation rule through its real route
- **WHEN** a v3 client requests full bootstrap and validates a body edit after an owner changes the page's extension class from pending to live
- **THEN** corrective guidance explains that the current live-class rule supersedes historical inactive-label teaching
- **AND** the client's exposed edit route requires a valid semantic unit without changing page bytes during validation
- **AND** bootstrap advertises no registry inspection route that v3 cannot call

### Requirement: Derived views classify admitted current status without persisting its meaning

Graph and artifact-role descriptors SHALL retain structural facts and raw status, without private lifecycle classes.
Relation-queue coverage SHALL classify the complete admitted graph metadata basis before selecting bounded source candidates.
Dreamer SHALL admit candidate paths and check current page eligibility before counting contributors toward existing caps.
Artifact-role coverage SHALL become unknown when its required status meaning is unavailable.
Vocabulary projection SHALL preserve physical pagination and classify admitted endpoints before emitting pairs.
Its empty-for-write shortcut SHALL use a status-neutral candidate superset.
New retained-input bindings SHALL reject class `superseded` after release; existing binding disclosure SHALL retain exact unit resolution independently of parent lifecycle disposition.

#### Scenario: A registry-only transition changes an already built graph queue
- **WHEN** an owner retires an extension used by source pages and then restores its prior registry version
- **THEN** the next relation queue updates source inclusion, exact coverage and truncation without page edits or a graph rebuild
- **AND** denied definitions make only the dependent queue unavailable

#### Scenario: Hidden hydration contributors do not consume visible capacity
- **WHEN** hidden retired or unfamiliar contributor pages precede visible contributors in graph order
- **THEN** they consume no contributor capacity and trigger no status-registry lookup
- **AND** the admitted hydration proposal matches the hidden-absent result

#### Scenario: A retained unit remains disclosable after its parent is superseded
- **WHEN** an admitted prior binding names an exact unit whose parent later becomes superseded
- **THEN** disclosure can still resolve the retained unit under existing release and identity checks
- **AND** a new binding cannot treat that parent as a current input
