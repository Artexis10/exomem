## ADDED Requirements

### Requirement: Compiled Altitude Is Rendered In Each Contender's Own Grammar
The harness SHALL support a `compiled` ingestion altitude in which the corpus
arrives through capture followed by compilation. Each adapter SHALL render the
neutral compile plan into its own product's grammar through the native-renderer
seam, and SHALL publish a per-conclusion parity report recording each conclusion
as represented, degraded, or unsupported. No adapter SHALL receive a plan
shaped by another product's API.

#### Scenario: Each contender compiles natively
- **WHEN** a run executes at compiled altitude
- **THEN** every adapter that declares the tier writes compiled conclusions
  through its own product surfaces, and its parity report accounts for every
  conclusion in the plan with nothing silently dropped

#### Scenario: A neutral record, not one product's shape
- **WHEN** an adapter renders a conclusion record
- **THEN** it derives its native representation from the conclusion, its cited
  sources and its lineage alone

### Requirement: An Adapter That Cannot Compile Is Excluded, Never Zeroed
An adapter SHALL declare whether it can honour the compiled tier. One that
cannot SHALL report `unsupported` for altitude-dependent dimensions and SHALL be
excluded from compiled-altitude comparison. A run that cannot apply the tier to
a contender SHALL refuse that comparison rather than compare unequal
configurations. Declining to compile SHALL never be recorded as a contender
loss.

#### Scenario: Model-requiring compilation is honestly unsupported
- **WHEN** a contender's only compile path requires a reasoning model, which the
  deterministic layer excludes
- **THEN** it declares the tier unsupported, its altitude-dependent dimensions
  report unsupported, and no zero is recorded against it

#### Scenario: Mixed-altitude comparison is refused
- **WHEN** a comparison contains runs at different ingestion altitudes
- **THEN** altitude-dependent dimensions are withheld with the altitudes named,
  and retrieval-property dimensions remain comparable

### Requirement: Provenance Measures Chain Preservation At Compiled Altitude
At compiled altitude the provenance dimension SHALL be scored against the
contender's own attribution surface rather than a harness-authored answer:
recall as the required sources present in the chain the system reports for a
conclusion, precision as the reported sources being within the oracle-permitted
set. Where a system reports no attribution surface, provenance SHALL report
unsupported rather than zero.

#### Scenario: A preserved chain scores
- **WHEN** a system is given a conclusion citing specific sources and later
  reports that conclusion's basis
- **THEN** provenance scores recall and precision against the oracle-permitted
  set for that conclusion

#### Scenario: A dropped chain fails rather than abstains
- **WHEN** a system stores the conclusion but reports none of its cited sources
- **THEN** provenance records a recall failure, because the chain was supplied
  and not preserved

### Requirement: Contradiction Is Measured Behaviourally Over Compiled Conclusions
At compiled altitude the contradiction dimension SHALL measure whether a system
surfaces a conflict between compiled conclusions asserting incompatible values
for the same claim. It SHALL be satisfiable without any numeric confidence
field, and SHALL NOT require generated hedging language from a contender that
has no generative step.

#### Scenario: Conflict surfaced
- **WHEN** two compiled conclusions assert incompatible values and the claim is
  queried
- **THEN** a system that surfaces the conflicting pair passes, and one that
  reports a single value as settled fails

#### Scenario: The dimension is reachable
- **WHEN** the ceiling contender runs at compiled altitude
- **THEN** it passes the contradiction dimension, proving the gate is
  satisfiable rather than structurally void
