## ADDED Requirements

### Requirement: A Scope May Deny Audiences It Does Not Name

A scope MAY declare that the standing default for an audience is the most restrictive
level rather than the most permissive one. Where a scope carries that declaration and an
item is a member of it, an audience for which no standing rule matches SHALL resolve to no
disclosure, instead of to full release.

The declaration changes the DEFAULT only. It SHALL NOT override an authored rule: where a
standing rule names the audience for that scope, that rule's ceiling applies exactly as it
does today. Grants SHALL continue to only raise, organisation caps SHALL continue to only
lower, and a declared purpose SHALL continue to only narrow.

The owner SHALL never be subject to the declaration. An owner locked out of their own
scope is a vault that has lost its own contents, which is not the confidentiality this
expresses.

Where an item is a member of several scopes and any one of them carries the declaration,
the restrictive default applies — a scope cannot be widened by adding an undeclared scope
alongside it.

A vault with no governance tree SHALL remain on the empty fast path, unaffected.

#### Scenario: an audience no rule names receives nothing

- **WHEN** an item belongs to a scope carrying the declaration and a request arrives from
  an audience for which no standing rule matches that scope
- **THEN** the decision is no disclosure
- **AND** the item is indistinguishable from one that does not exist

#### Scenario: a newly minted audience id is denied by default

- **WHEN** a principal's credential is rotated so it resolves to an audience id that
  appears in no policy document, and it requests an item in a declared scope
- **THEN** the decision is no disclosure
- **AND** the outcome is identical to the pre-rotation audience having been unnamed

#### Scenario: an authored rule still governs the audience it names

- **WHEN** a standing rule names an audience for a declared scope with a ceiling above no
  disclosure
- **THEN** that audience receives the rule's ceiling
- **AND** the declaration does not lower it

#### Scenario: a grant still raises above the default

- **WHEN** an audience unnamed by any standing rule holds a grant for a declared scope
- **THEN** the grant's ceiling applies
- **AND** the declaration does not suppress it

#### Scenario: an organisation cap still lowers

- **WHEN** an organisation cap applies to a declared scope alongside a rule permitting a
  higher level
- **THEN** the lower of the two applies, unchanged by the declaration

#### Scenario: the owner reads a declared scope

- **WHEN** the owner requests an item in a declared scope for which no rule names the owner
- **THEN** the owner receives full release

#### Scenario: one declared scope denies across an overlapping undeclared scope

- **WHEN** an item belongs to both a declared scope and an undeclared scope, and the
  audience is named by no standing rule
- **THEN** the decision is no disclosure

#### Scenario: an undeclared scope keeps today's default

- **WHEN** an item belongs only to scopes carrying no declaration and no standing rule
  matches the audience
- **THEN** the decision is full release, exactly as before this change

#### Scenario: inspection explains a default denial

- **WHEN** the owner explains the decision for an item withheld by the declaration
- **THEN** the explanation identifies the declaring scope
- **AND** it does not attribute the outcome to a standing rule that does not exist
