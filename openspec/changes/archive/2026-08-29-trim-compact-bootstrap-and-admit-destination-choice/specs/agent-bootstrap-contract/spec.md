## MODIFIED Requirements

### Requirement: The agent contract teaches pre-write destination choice

The bootstrap contract SHALL teach, in both the full and compact profiles, that
choosing the destination is part of writing, not a reaction to advisories: when
a coherent durable thread emerges in conversation that sits outside the current
page's declared scope, the agent searches for a focused existing destination or
creates and links a focused child note, rather than letting the current page
accumulate structural debt until a detector fires. The compact profile MAY
carry a condensed wording, but SHALL state both halves of the rule: routing
happens at write time, and post-write structural advisories are the safety
net for missed routing, not the primary mechanism.

#### Scenario: A divergent thread gets a home before the detector must speak

- **WHEN** the bootstrap contract teaches capture routing in any profile that carries teaching
- **THEN** it states that an emerging coherent durable thread outside the current page's declared scope is routed to a focused existing or new destination at write time
- **AND** it states that post-write structural advisories are the safety net for missed routing, not the primary mechanism

#### Scenario: Admission is paid for by trimming, not by the ceiling

- **WHEN** the compact payload carries the destination-choice teaching
- **THEN** the compact payload remains at or below its unchanged byte ceiling with at least the codified warning headroom to spare
- **AND** the compact profile remains measurably smaller than full under the existing saving-ratio floor
