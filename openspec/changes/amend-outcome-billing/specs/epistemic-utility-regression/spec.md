## MODIFIED Requirements

### Requirement: Outcome results are labelled internal diagnostics

Every outcome report SHALL carry the label "internal diagnostic", because the actors run on a subscription login or on API credits and not under the suites' official protocols. A development or panel report SHALL state that it is instrument evidence. Only a held-out report under an acknowledged amendment SHALL state that it is decision evidence. No outcome figure SHALL be presented as a published suite result.

#### Scenario: A development round shows a large gain

- **WHEN** a development report shows a paired difference whose interval excludes zero
- **THEN** the report still reads as instrument evidence
- **AND** it supports no comparative claim

### Requirement: Outcome actors and judges are isolated

Outcome actors SHALL run headless on the billing source that their run declares, either the Claude subscription login or Claude API credits, with project-only settings, a strict MCP configuration, an out-of-tree working directory, auto memory off and path-scoped file permissions. Their environment SHALL NOT reach the live personal vault, the live service state or the user's MCP configuration, and each arm's Exomem state SHALL live inside its arm directory. Every actor session and every Claude judge call SHALL record the credential source it reports, and a session that reports a source other than the declared one SHALL be a harness fault. The billing source SHALL NOT change the evidence class of a held-out report once the amendment that allows both sources is acknowledged; until then a held-out report billed to credits SHALL read as instrument evidence. A judge SHALL see an opaque item id, the task, the key or criteria and the answer, and SHALL NOT see the arm, the vault or the recall packet. The judgement file SHALL record the judge's model id, and every judged report SHALL name whether the judge shares the actor's model family. When the decision judge shares it, a judge from another family SHALL re-judge a sample once it has room, and the report SHALL give the agreement as description. A held-out report judged by a model other than the frozen decision judge, or a held-out run whose actor model or effort differs from the frozen one, SHALL NOT read as decision evidence.

#### Scenario: The parent shell points at the live vault

- **WHEN** the harness starts an actor from a shell whose environment names the live vault and service state
- **THEN** the actor's environment carries none of those values
- **AND** its Exomem state resolves inside the arm directory

#### Scenario: The decision judge is from the actor's family

- **WHEN** an f36 or f37 decision report is judged by a Claude model while the actor is a Claude model
- **THEN** the report names the judge family as the same as the actor's
- **AND** a later cross-family sample re-judge adds its agreement without changing the decision

#### Scenario: A held-out run is billed to API credits

- **WHEN** a gated held-out run declares credits, every session reports credits, and its actor and judge are the frozen ones
- **THEN** the report reads as decision evidence once the billing amendment is acknowledged
- **AND** it reads as instrument evidence while that amendment is pending

#### Scenario: A session reports another credential source than the run declared

- **WHEN** a run declares the subscription and an actor session reports an API key as its credential source
- **THEN** the session is a harness fault under the suite's fault rules
- **AND** the run file records the declared and the observed source
