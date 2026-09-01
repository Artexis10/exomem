## ADDED Requirements

### Requirement: Interactive Corpus-Context Joins Are Bounded

A caller that finds a corpus-context build already registered for its cache key SHALL
join that flight through one fixed 2.0-second wait, not by the owner's build duration.
The bound MUST be an internal constant with no configuration surface, environment
override, or per-call parameter. If the joined flight is already settled when the caller
observes it, the caller SHALL consume its result or error immediately without invoking
the timed wait or consuming the 2.0-second budget. On expiry the waiter SHALL stop
waiting, release any admitted waiter capacity, and return a typed deferred outcome.
Flight ownership, cache-key derivation, census capture, `same_inputs` recomputation, and
the owner's own build path MUST remain unchanged. A waiter that expires MUST NOT cancel,
disturb, or invalidate the owner's in-flight build.

#### Scenario: A slow owner does not stall an interactive waiter

- **WHEN** a corpus-context build is in flight and a second interactive request joins it
- **AND** the owner has not completed within the 2.0-second bound
- **THEN** the waiter returns a typed deferred outcome within the 2.0-second bound
- **AND** the owner's build continues uninterrupted to its own completion
- **AND** the waiter's admitted capacity is released

#### Scenario: A fast owner is still joined normally

- **WHEN** a corpus-context build is in flight and completes within the 2.0-second bound
- **THEN** the joining waiter returns the owner's result exactly as it does today
- **AND** no deferred outcome is produced

#### Scenario: An already-settled flight does not wait

- **WHEN** a joining caller observes that the corpus-context flight is already settled
- **THEN** it consumes the owner's result or error immediately
- **AND** it does not invoke the timed wait or spend the 2.0-second budget

#### Scenario: Changed inputs still recompute rather than defer

- **WHEN** a waiter joins a flight whose census, registry identity, or language identity
  does not match its own
- **AND** the flight completes within the 2.0-second bound
- **THEN** the waiter recomputes its own corpus context as it does today
- **AND** the bound does not alter that recomputation path

### Requirement: A Bound Never Launders A Failure

The bounded join SHALL return its deferred outcome only when the wait expires. When the
owner's build fails, the waiter MUST continue to raise that build error. A deferred
outcome and a build failure MUST be distinguishable by the caller and MUST NOT share a
representation.

#### Scenario: Owner failure propagates as failure

- **WHEN** a corpus-context flight completes with an error inside the 2.0-second bound
- **THEN** the joining waiter raises that error
- **AND** it does not return a deferred outcome

### Requirement: A Deferred Outcome Is Visible In The Default Projection

A bounded corpus-context result SHALL be distinguishable from a completed one in the
default response projection, without requiring an expanded or diagnostic detail level.
A deferred outcome MUST NOT be byte-identical to a completed outcome.

#### Scenario: Default response distinguishes deferred from complete

- **WHEN** an interactive request returns a bounded corpus-context result
- **THEN** the default response projection marks the corpus context as deferred
- **AND** the marker is present without requesting full or legacy detail

### Requirement: Every Unbounded Join Is Bounded Or Declared

Each blocking join on a background flight or worker in the request path SHALL be either
bounded by an interactive budget or explicitly declared background-only with a recorded
reason. A test SHALL enumerate these call sites and fail when a site is neither bounded
nor declared, so that adding a new unbounded join is a test failure rather than a latent
regression.

#### Scenario: A new unbounded join fails the suite

- **WHEN** a blocking join with no timeout is added on a request-reachable path
- **AND** it carries no background-only declaration
- **THEN** the enumeration test fails and names the offending call site
