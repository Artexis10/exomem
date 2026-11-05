## ADDED Requirements

### Requirement: User methods are live portable knowledge

Supported clients SHALL obtain applicable user-authored methods through the same governed runtime projection. A method edit SHALL NOT require reinstalling a static skill package or using client-native memory. The shared skill SHALL explain selection, source reads, current user overrides and revision refresh without loading every method on every turn.

#### Scenario: A fresh generic client needs a method
- **WHEN** a client has only the portable Exomem contract and activates a relevant task
- **THEN** it can discover and read the user's current method through ordinary public operations

#### Scenario: The task is unrelated
- **WHEN** the next turn is unrelated to a previously used method
- **THEN** the client does not apply that method merely because it was previously delivered

### Requirement: Reading methods does not delegate new authority

Clients SHALL distinguish governed method knowledge, structured workflow policy and retrieved source material. Method content SHALL NOT authorize external actions, change access policy, install executable code or assert completion. Persistent method edits SHALL use the existing authorized writer and its stale-source guard.

#### Scenario: A method requests an external send
- **WHEN** a stored method describes sending a message but the current task grants no send authority
- **THEN** the client retains the method as guidance and does not treat it as approval to send

#### Scenario: A direct edit races an agent update
- **WHEN** a user edits a method after an agent read it
- **THEN** the ordinary stale-source guard preserves the user's edit and requires reconciliation before the agent updates it
