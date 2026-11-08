# portable-agent-contract Specification

## Purpose
Give every client the same semantic-authoring contract from one versioned runtime source, whether via MCP-only bootstrap or plugin and skill packages. Runtime and human projections cannot drift, and public authoring artifacts stay generic.

## Requirements

### Requirement: Semantic Authoring Contract Has One Versioned Runtime Source

The package SHALL expose one deterministic, versioned semantic-authoring contract
with a content digest. It SHALL contain the exact one-line compact syntax,
optional suffix order, canonical section, open-category and governed-kind
distinction, canonical rich heading plus optional leading `id`, `category`,
`tags`, `context`, and `relations` metadata, body/blank-line and heading-boundary
rules, active compiled-note minimum, exact applicability/exemptions, preferred
typed routes, Tier-2 behavior, stable findings, and compact/rich remediation.
It SHALL state that unit coverage and relation disposition are independent.
Contract construction SHALL NOT inspect a vault or require a model.

#### Scenario: Contract is vault-independent
- **WHEN** the contract is rendered in two environments with different or absent vaults
- **THEN** its normalized semantic fields and version are byte-identical and contain no vault-derived paths, titles, excerpts, project keys, or identifiers

#### Scenario: Contract version and digest track normative change
- **WHEN** any syntax, applicability, error, or required authoring behavior changes
- **THEN** the contract version, content digest, and all deterministic projections change together

### Requirement: MCP-Only Distribution Is Sufficient

A generic client with only the MCP server's tool list and portable `compact`, `full`, or `diagnostics` bootstrap operation SHALL receive enough information to create a compliant compiled note. Those generic profiles SHALL include the complete minimum semantic-authoring object, and authoring-tool descriptions SHALL include the exact compact syntax, `## Observations` location, open-category rule, and required-minimum/remediation that applies to that tool. A valid `session` profile is the attested exception: it MAY omit generic semantic-authoring, Records, Planning, and front-door teaching only after the matching installed skill contract has been accepted, while retaining the live due-state/family-disposition post-write guidance required to interpret returned state.

#### Scenario: Session profile relies on the attested installed skill
- **WHEN** a client presents the matching installed skill contract with `profile="session"`
- **THEN** the response omits generic semantic-authoring teaching
- **AND** portable profiles remain complete for clients without that installed skill

#### Scenario: Generic MCP client can author without a skill
- **WHEN** a client receives tool schemas and calls default `bootstrap()` without an installed skill or repository instructions
- **THEN** it can distinguish category, tag, and kind; choose compact or rich form; satisfy the minimum; and remediate a refusal through the preferred write route

#### Scenario: Tier-2 schema warns at the escape-hatch moment
- **WHEN** a client inspects `manage_memory_file` create, overwrite, or append guidance
- **THEN** it learns that compiled destinations receive the same semantic contract and is directed to `remember` or `replace_memory` when those typed routes fit

### Requirement: Plugin And Skill Distribution Is Sufficient

The committed plugin, filesystem installs, and uploadable/default skill packages
SHALL be generated from the generic scaffold and SHALL ship the core skill plus
every declared workflow skill. The core skill SHALL contain the complete semantic-authoring
contract. Every separately installable workflow archive capable of compiling,
replacing, or curating an active note SHALL contain the complete concise minimum
contract itself; a reference to an absent core skill SHALL fail packaging. Bundled
plugin workflows MAY additionally reference the bundled core section. No packaged
skill SHALL depend on repository-only instructions, bootstrap availability, or a
private overlay.

#### Scenario: Fresh plugin install has the whole contract
- **WHEN** the plugin is installed into a client with no repository checkout and no personal skill directory
- **THEN** its bundled core and workflow skills can route and author a compliant compiled note using the configured MCP server

#### Scenario: Standalone workflow archive is independently usable
- **WHEN** one authoring workflow archive is installed by itself with no core skill, repository checkout, or bootstrap response
- **THEN** that workflow contains the minimum contract needed to choose compact versus rich form and produce a compliant active note

#### Scenario: Filesystem install is independently usable
- **WHEN** the default core and workflow skills are installed into a clean skill directory with no repository checkout, pre-existing personal skills, or bootstrap response
- **THEN** the installed files alone contain the minimum contract and can route and author a compliant active note

#### Scenario: Default package excludes personal overlays
- **WHEN** public plugin sync or default skill packaging runs
- **THEN** it reads only the generic scaffold and emits no private project registry, local path, vault content, or maintainer-specific instruction

### Requirement: Runtime And Human Projections Cannot Drift

Bootstrap output, concise authoring-tool descriptions, the bounded core-skill
authoring section, authoring workflow skills, generated capability docs, committed
plugin copies, filesystem installs, wheel/sdist contents, and generic skill
archives SHALL be checked against the same normalized contract version and
content digest. CI SHALL regenerate and unpack public artifacts before comparison.
The existing plugin-tree sync check SHALL remain authoritative for packaged file
parity. A missing support file, missing rule, conflicting rule, stale version or
digest, or changed exact grammar SHALL fail deterministic validation.

#### Scenario: Skill omits the required section
- **WHEN** a scaffold or packaged plugin edit removes `## Observations` or changes the exact compact grammar without changing the canonical contract
- **THEN** the contract parity test fails and names the drifted projection

#### Scenario: Generated surfaces remain aligned
- **WHEN** command schemas, OpenAPI, capability documentation, and plugin files are regenerated
- **THEN** each exposed semantic-authoring projection matches the canonical version and the committed fidelity fixtures change only intentionally

#### Scenario: Built artifacts preserve contract identity
- **WHEN** wheel, sdist, filesystem install, plugin package, and every generic skill archive are built and unpacked in CI
- **THEN** each applicable artifact contains the expected contract bytes, version, digest, and required support files

### Requirement: Public Authoring Artifacts Remain Generic

Public package sources, scaffold content, workflow skills, plugin and marketplace
files, documentation, OpenSpec artifacts, examples, tests, fixtures, example-
bearing scripts, generated schemas/docs, and all built public archives SHALL use
synthetic generic content and SHALL NOT incorporate private-vault notes,
identifiers, local paths, organizations, people, project registries, or
confidential facts. Privacy validation SHALL inspect distributable inputs and
unpacked generated public outputs without encoding a particular private corpus as
a public fixture or allowlist. Diagnostics SHALL report only rule, file, and line,
never matched source content.

#### Scenario: Regression fixture is synthetic
- **WHEN** compact coverage, rich nesting, Tier-2 parity, or plugin packaging is tested
- **THEN** every page, category, path, relation, and project key used by the test is an invented generic example

#### Scenario: Public build does not read a live vault
- **WHEN** public plugin sync, documentation generation, schema snapshots, or default skill packaging runs
- **THEN** the build succeeds from repository inputs alone and cannot import a live-vault overlay

#### Scenario: Archive members are inspected after unpacking
- **WHEN** a public archive is built
- **THEN** privacy validation checks member names and supported text content, requires explicit provenance handling for binary or unsupported members, and does not silently skip them

#### Scenario: New public formats require coverage
- **WHEN** a new file format appears in a distributable root
- **THEN** the privacy gate fails until that format has explicit scanning or provenance handling

#### Scenario: Personalized packaging cannot feed a public build
- **WHEN** explicit private-output packaging uses a local overlay
- **THEN** its output remains outside tracked and release paths and cannot be consumed by plugin sync, public docs, schemas, tests, fixtures, wheel/sdist, or generic archives

#### Scenario: Distribution privacy gate rejects leaked context
- **WHEN** a distributable input or generated public artifact contains content outside the generic contract boundary
- **THEN** the privacy gate fails before release and remediation removes or genericizes the content rather than adding a private token to an allowlist

### Requirement: Workflow contract projections share one resolved identity

The code-owned workflow invariant kernel, built-in fallback, schema version, resolver semantics, and fixed-template renderer SHALL have one normalized runtime source and content digest. Bootstrap, command descriptions, generated capability documentation, plugin copies, filesystem installs, wheel/sdist contents, and generic skill archives SHALL be checked against that identity. The generic scaffold SHALL remain the repository's hand-authored, deliberately generic skill source rather than a generated runtime projection; parity tests SHALL pin its declared workflow contract version, operation names, and invariant statements to the machine identity. Vault-authored contract instances SHALL remain personalized data and SHALL NOT enter public package generation or generic fixtures.

#### Scenario: Generic and personalized projections agree
- **WHEN** the same canonical workflow contract is resolved through MCP, REST, CLI, and an installed skill-enabled client
- **THEN** each surface reports the same contract identity, fingerprint, machine decision, provenance, and invariant boundary while presentation may differ only by documented profile bounds

#### Scenario: Public package remains generic
- **WHEN** public artifacts are built on a machine whose vault contains personal workflow contracts
- **THEN** the artifacts contain only the generic invariant/resolver contract and no personal tool names, projects, contract titles, paths, or identifiers

#### Scenario: Human renderer drift fails deterministically
- **WHEN** a renderer or skill edit changes the meaning of a workflow field without changing the normalized contract identity
- **THEN** parity validation fails and names the drifted projection

### Requirement: The shipped scaffold teaches the bounded episode-completeness pass

The shipped generic scaffold's engagement reference SHALL carry the same bounded
episode-completeness pass the prominence capture contract carries, presenting its
classes as examples and never as a closed enumeration, and SHALL state that a
write response's advisory is a prompt for that pass rather than a demand that
something be written. The generated local plugin copies SHALL be regenerated from
the scaffold so the shipped sources stay byte-identical.

The scaffold SHALL stay generic: no person, client, supplier, venue or product
name may appear in it or anywhere in the shipped package source.

The copyable custom-instruction blocks SHALL carry the same pass ONLY once their
carrier has room for it. They are capped at 1,500 bytes because that is the
custom-instruction field they are pasted into, and the two prominent blocks
measure 1,484 and 1,495 bytes. No wording of the clause fits inside that headroom,
and an existing capture class SHALL NOT be shortened to make room. Until a trim
frees the room, those blocks SHALL continue to direct the client to the compact
bootstrap payload, which carries the clause and its handling guidance in full.

#### Scenario: The scaffold carries the clause

- **WHEN** the shipped scaffold's engagement reference is read
- **THEN** it names the bounded pass, marks its classes as examples, and keeps
  every stepping-stone rule it carried before

#### Scenario: The generated plugin copies stay in sync

- **WHEN** the local plugin skill copies are compared against the scaffold
- **THEN** they are byte-identical to what the scaffold packages

#### Scenario: A capped carrier is not paid for by a trim

- **WHEN** a copyable instruction block has fewer bytes of headroom than the
  clause needs
- **THEN** the block is left unchanged rather than shortened, and the doctrine is
  served through the bootstrap payload the block already directs the client to

#### Scenario: The scaffold stays generic

- **WHEN** the shipped scaffold is scanned by the public-artifact privacy gate
- **THEN** the added clause introduces no personal, client, supplier or product name

### Requirement: Portable carriers deliver the bounded recurring-identity check
The generic scaffold, installed core skill, and hookless custom-instructions projection SHALL carry the same capability-honest recurrence cadence as bootstrap. At balanced and maximal prominence, an active agent SHALL request the explicit `entity_recurrence` category at most once ordinarily per bootstrapped conversation or session during the first user turn after successful bootstrap, after primary task work and before the final response, including when that interaction is not entity-topical. It MAY perform exactly one additional general state recheck after an Entity, accepted relation, or entity-type registry mutation outside hydration continuation. Each ordinary or general read SHALL request one audit pass and at most three candidates.

After each separately confirmed hydration batch returns a terminal mutation receipt, the agent MAY perform exactly one immediate recheck bound to the same identity. Rechecks after processed batches 1–7 may expose at most the next eight contexts; the eighth recheck after processed batch 8 is closure-only and exposes no ninth batch. Another recheck requires another fresh confirmation and terminal receipt. The chain SHALL stop on the specified state/outcome stops or after eight mutated batches and eight identity rechecks in one session; remaining work stays open for the next session's ordinary read. Off and light SHALL remain explicit-request-only.

The generic scaffold and installed core skill SHALL teach resolve-before-create, ambiguity stop, hydrate-before-duplicate, and governed unknown-kind registration without listing a closed ontology. The hookless custom-instructions projection is bounded by the 1,500-byte web custom-instructions limit: it SHALL carry the cadence and the unavailable-skip rule and SHALL defer the decision order to the bootstrap payload it already instructs the agent to follow, and that payload SHALL carry the order. When the exported surface cannot request the category, every carrier SHALL mark the check unavailable and skip it rather than inventing a command, local scan, model inference, or due-state result.

#### Scenario: Hookless agent checks on an ordinary interaction
- **GIVEN** a clean balanced client with only the generic portable contract and an explicit category-read surface
- **WHEN** the first normal maintenance boundary follows a non-topical ordinary user prompt
- **THEN** the agent requests `entity_recurrence` once with a limit of three and surfaces one returned candidate for semantic review
- **AND** a frequency-matched incidental twin is absent

#### Scenario: Later ordinary prompts do not rescan
- **GIVEN** the session already spent its ordinary recurrence read
- **WHEN** later prompts arrive without a qualifying mutation
- **THEN** the carrier authorizes no second recurrence scan in that session

#### Scenario: Post-mutation recheck observes lifecycle state
- **GIVEN** the ordinary recurrence read was spent and an Entity, accepted relation, or registry entry is then committed
- **WHEN** the mutation receipt is terminal
- **THEN** the agent may perform one immediate general recurrence recheck to observe closure or transition
- **AND** no second general read is authorized

#### Scenario: Hydration continuation is user-paced and session-bounded
- **GIVEN** a hydration identity has more than one batch of disconnected contexts
- **WHEN** each separately confirmed batch reaches a terminal mutation receipt
- **THEN** exactly one same-identity recheck may expose the next batch and then pauses for fresh confirmation
- **AND** after eight mutated batches the eighth recheck is closure-only and any remainder stays open for the next session rather than exposing batch nine or causing a background loop

#### Scenario: Portable carrier cannot overstate capability
- **GIVEN** a hookless client without an explicit category-read surface
- **WHEN** the recurrence boundary arrives
- **THEN** the carrier marks the check unavailable and performs no substitute scan or semantic inference

#### Scenario: Hookless projection stays within the web byte cap
- **GIVEN** the maximal and balanced copyable custom-instruction blocks
- **WHEN** each is measured as UTF-8 bytes
- **THEN** each is at most 1,500 bytes
- **AND** each names the once-per-conversation cadence and the unavailable-skip rule
- **AND** each defers the decision order to bootstrap rather than restating it

### Requirement: Portable capture carriers preserve the personal baseline predicate
The hand-authored scaffold, capture workflow skill, relevant operations reference, prominence rendering and documentation, the activation capture-policy block, and generated Claude Code copies SHALL carry one coherent generic personal-baseline predicate. Each proactive carrier SHALL preserve both eligibility discriminators, at least one eligible route, and the quiet negative boundary. The capture reminder SHALL NOT carry the predicate: it SHALL remain an advisory and structural check rather than becoming a semantic classifier.

#### Scenario: Carrier parity is semantic
- **WHEN** portable carriers are checked
- **THEN** each proactive carrier conveys stability or recurrence, reusable comparison value, semantic routing, and a casual-trivia negative even if sentence wording differs

#### Scenario: Structural reminder does not decide meaning
- **WHEN** the capture hook observes an ordinary interaction boundary
- **THEN** it emits only a bounded advisory check and neither classifies content nor invokes a semantic model

#### Scenario: Public artifacts remain generic
- **WHEN** scaffold and generated or installed plugin artifacts are built
- **THEN** privacy and leak gates prove that all fixtures, examples, and instructions use synthetic content

#### Scenario: Generated copies cannot drift
- **WHEN** a hand-authored source carrier changes
- **THEN** sync checks fail until every defined derived copy is regenerated from its source
