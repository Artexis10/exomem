## ADDED Requirements

### Requirement: Access-policy loading fails closed on transient errors

A transient failure to stat or read the access-policy file SHALL NOT install or cache a policy more permissive than the last successfully loaded one, and SHALL NOT change the access fingerprint used in recall identity. Transient errors reuse the last successfully loaded policy and fingerprint regardless of whether the stat signature moved - reuse never widens visibility, and convergence to the changed content happens at the next successful read; when no successful load exists, the system SHALL treat every path as excluded (fail closed) until a read succeeds. A failure in the read position after a successful stat - including the file vanishing between stat and read - is a transient error, never a policy change; only an absence observed at stat time is the genuine missing-policy identity. Recall identity SHALL only change when the policy content provably changed.

#### Scenario: A transient read error cannot widen visibility

- **WHEN** reading the access-policy file raises a transient error while a previously loaded policy exists
- **THEN** the previously loaded policy and its fingerprint remain in effect
- **AND** no cache entry is installed that outlives the error

#### Scenario: No successful load yet fails closed

- **WHEN** the access-policy file exists but has never been successfully read in this process
- **AND** reading it raises a transient error
- **THEN** access-gated serving treats all paths as excluded until a read succeeds

#### Scenario: A transient blip does not flip recall identity

- **WHEN** a stat or read blip occurs with the policy file's content unchanged
- **THEN** the recall identity and its access fingerprint are unchanged
- **AND** no recall generation advances and no admission refusal results

#### Scenario: A file vanishing between stat and read is transient

- **WHEN** the access-policy file exists at stat time and vanishes before the read completes
- **THEN** the previously loaded policy and its fingerprint remain in effect
- **AND** only an absence observed at stat time transitions to the missing-policy identity

### Requirement: A managed recall follower declines within a bounded wait

A refusal has to come back fast enough to be a refusal. When a recall
resolver build is already in flight, a managed caller - one forbidden from
building its own resolver inline - SHALL NOT wait on the leader longer than
a small bounded follower window before declining with a `retry_after_ms`
hint; it never inherits the leader's full build window. Only callers
permitted to fall back to building their own resolver (warm-up, CLI, cold
maintenance) may wait out the leader's full build bound, because for them
waiting is cheaper than duplicating the whole-vault walk that single-flight
exists to prevent. The wait selection SHALL be a single decision point so
the two bounds cannot silently converge.

#### Scenario: A managed follower refuses quickly instead of hanging

- **WHEN** a managed recall request finds a resolver build in flight
- **THEN** it declines within the bounded follower window with `retry_after_ms`
- **AND** the refusal names the gate as `resolver_build_wait` with the wait actually spent

#### Scenario: A fallback caller still waits out the leader

- **WHEN** a fallback-permitted caller finds a resolver build in flight
- **THEN** it waits up to the full build bound for the leader's resolver
- **AND** it does not duplicate the leader's build while the leader publishes within that bound

### Requirement: Retrieval warming refusals name their gate from a closed vocabulary

Every retrieval warming refusal SHALL carry a machine-readable `site`
discriminator identifying the exact gate that refused, drawn from a single
closed vocabulary declared in code, and SHALL carry the wait actually spent
(`waited_ms`) when the refusal followed a bounded wait. The vocabulary is
closed at the constructor: constructing a warming refusal with a site
outside the vocabulary SHALL fail, so no raise site can introduce an
undeclared or content-bearing discriminator. `site` and `waited_ms` SHALL
be content-free - never a path, a query, or a backend name - and SHALL
project unchanged through the REST and MCP error envelopes. The public API
schema SHALL express `site` as an enumeration derived from the declared
vocabulary rather than a hand-copied list, and the vocabulary SHALL be
bidirectionally checked against the raise sites: every declared site is
used, and every used site is declared.

#### Scenario: An out-of-vocabulary site is refused at construction

- **WHEN** a warming refusal is constructed with a site outside the declared vocabulary
- **THEN** construction fails before any envelope is produced

#### Scenario: The envelope carries the discriminator unchanged

- **WHEN** a retrieval warming refusal reaches a REST or MCP client
- **THEN** the error body carries `site` from the closed vocabulary and an integer `waited_ms`
- **AND** neither field carries a path, a query, or a backend name

#### Scenario: Dead and undeclared vocabulary entries are both defects

- **WHEN** the declared vocabulary and the raise sites are compared
- **THEN** a declared site no raise site uses fails the check
- **AND** a raise site using an undeclared site fails the check
