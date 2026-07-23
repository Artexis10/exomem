## ADDED Requirements

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

A generic client with only the MCP server's tool list and bootstrap operation
SHALL receive enough information to create a compliant compiled note. Every
bootstrap profile SHALL include the complete minimum semantic-authoring object,
and authoring-tool descriptions SHALL include the exact compact syntax,
`## Observations` location, open-category rule, and required-minimum/remediation
that applies to that tool.

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
