## ADDED Requirements

### Requirement: Project-key domains are declared through a governed write

`schema_memory` SHALL inspect, propose and save the domains of project keys. A save SHALL require the current registry hash and an audit reason. It SHALL refuse a stale hash. It SHALL refuse a domain value that does not resolve through the domains resolver, unless the same save registers that value through the domains registry writer. Each declared domain SHALL carry an origin, `inferred` or `reviewed`. A declaration without a recorded origin SHALL read as `reviewed`.

#### Scenario: A stale save is refused

- **WHEN** an agent saves project-key domains with a hash older than the registry's current hash
- **THEN** the save is refused and the registry is unchanged

#### Scenario: A new domain registers in the same save

- **WHEN** a save declares a domain key that the domains registry does not hold and includes its registration
- **THEN** the domain is registered and the project key's pages join that topic on the next map

#### Scenario: A hand-written declaration counts as reviewed

- **WHEN** a person wrote `domain: ops` for a key by hand
- **THEN** `inspect` reports that declaration with origin `reviewed`

### Requirement: The agent infers domains and the server only supplies evidence

When a project key has member pages and no declared domain, or a declaration that does not resolve, ordinary work SHALL expose a bounded consideration in the `project-domain/1` vocabulary family. The consideration SHALL carry the key's folder, category, page count, a bounded sample of member titles with their catalogue descriptions, and the existing domains with their descriptions and aliases. The active agent SHALL choose the domain. The server SHALL NOT call a model and SHALL NOT propose a domain from word lists or text patterns. An unchanged consideration SHALL NOT be repeated, and a change to its evidence fingerprint SHALL allow it again.

#### Scenario: An undeclared key is considered once

- **WHEN** a key with member pages has no domain and its evidence does not change
- **THEN** the consideration appears once and is not repeated in later turns

#### Scenario: The agent declares an inferred domain

- **WHEN** the agent reads the consideration and saves a domain for the key
- **THEN** the declaration carries origin `inferred` and the key's pages join that topic at once

### Requirement: A person can accept, rename or reject a declaration

`inspect` SHALL list every declaration with its origin. A person's accept SHALL set origin `reviewed`. A rename SHALL register an alias in the domains registry and keep the key's membership. A reject SHALL remove the declaration and record the domain in the key's rejected list. The agent SHALL NOT propose a rejected domain for that key again.

#### Scenario: A rejected domain is not proposed again

- **WHEN** a person rejects an inferred domain for a key
- **THEN** the declaration is removed and no later consideration for that key offers that domain

#### Scenario: A rename keeps membership

- **WHEN** a person renames an inferred domain
- **THEN** the old key becomes an alias of the new one and the key's pages stay members of the topic

### Requirement: Onboarding shows inferred domains for review

Vault adoption and the first-session bootstrap SHALL run domain inference for undeclared project keys. They SHALL then show the person one list with each key, its domain, the declaration's origin and a few member titles. The person SHALL be able to accept all, change single rows, or skip. Skipping SHALL leave the declarations `inferred` and in effect.

#### Scenario: A skipped review keeps the map working

- **WHEN** the person skips domain review during onboarding
- **THEN** the inferred declarations stay in effect and remain listed for later review
