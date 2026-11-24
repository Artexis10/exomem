## ADDED Requirements

### Requirement: A bounded automatic restructure follows proactive capture
The `structured-collections` capability defines the automatic-restructure bounds. A Records or Planning `restructure` that the agent starts on its own and that meets every bound SHALL be a `proactive_capture` action and SHALL follow its served disposition. An undo of an automatic restructure SHALL follow the same class.

#### Scenario: Silent capture applies a bounded restructure without a question
- **WHEN** `proactive_capture` is `silent` and a later fact needs a new key that every row can hold
- **THEN** the agent rekeys the collection without asking, and its reply says nothing about the rekey

### Requirement: A restructure follows the served capture disposition
Under `silent` the agent SHALL apply a bounded restructure, and SHALL tell the user only when it alters what they get. Under `advisory` the agent SHALL propose, in plain words, a restructure that alters what the user gets, and SHALL apply it after one inline confirmation. It SHALL NOT start one that alters nothing the user gets. Under `off` the agent SHALL initiate no restructure, while a restructure the user asks for SHALL still be served.

#### Scenario: Advisory capture proposes a change the user would notice
- **WHEN** `proactive_capture` is `advisory` and two of the user's lists overlap
- **THEN** the agent asks in plain words whether to combine them, and combines them only after the user confirms

### Requirement: A restructure outside the bounds stays confirm-required
A restructure outside the automatic bounds SHALL remain an action of the `restructure_execution` class: confirm-required at every prominence, satisfiable by one inline confirmation of the named proposal, never by a standing delegation. Bootstrap SHALL serve this classification with the envelope.

#### Scenario: A restructure outside the bounds stays confirm-required
- **WHEN** an automatic restructure would change a field the user designed
- **THEN** the change is `restructure_execution` and needs one inline confirmation at every prominence

### Requirement: Retirement is the automatic alternative to deletion
Permanent deletion of a collection, a row or a value SHALL remain `restructure_execution` at every prominence, with every server-side confirmation it has today, and SHALL never be automatic. Retirement SHALL be the automatic alternative to deletion.

#### Scenario: An obsolete list is retired, never deleted
- **WHEN** `proactive_capture` is `silent` and the agent judges that a list is obsolete
- **THEN** the agent may retire it, every row stays recoverable, and nothing is deleted without the user's confirmation
