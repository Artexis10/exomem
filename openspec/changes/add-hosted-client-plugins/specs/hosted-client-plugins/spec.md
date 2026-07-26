## ADDED Requirements

### Requirement: One Canonical Definition Renders Supported Hosted Packages

The system SHALL maintain one canonical Exomem Hosted plugin definition and SHALL deterministically render installable Claude and OpenAI artifacts from it. Both artifacts MUST represent the same product identity, production Hosted MCP resource, `hosted-alpha-agent-v1` profile, Hosted skill set, and release version while using only manifest fields and file layouts accepted by the target platform.

#### Scenario: Claude and OpenAI candidates are rendered

- **WHEN** maintainers render one Hosted plugin release for the supported platforms
- **THEN** the output contains a validator-clean Claude package and a validator-clean OpenAI package
- **AND** both packages bind the same endpoint, profile, skills, and release identity through their platform-specific formats

#### Scenario: The same inputs are rendered twice

- **WHEN** identical canonical definition, skill sources, profile contract, platform schemas, and release inputs are rendered twice
- **THEN** the normalized files, package locks, and artifact digests are byte-for-byte identical

#### Scenario: A renderer needs unsupported manifest metadata

- **WHEN** compatibility metadata is not accepted by a platform manifest schema
- **THEN** the renderer stores it in the package lock or promotion record instead of emitting an invalid manifest field
- **AND** no platform-specific behavior is silently dropped

### Requirement: Installation Requires No Manual Exomem Configuration

Each production Hosted package SHALL contain the fixed HTTPS Exomem Hosted MCP resource and SHALL use the host's native OAuth connection flow. A valid invitee MUST be able to install and authorize the package without pasting an MCP URL, installing Exomem or Python locally, setting an environment variable or vault path, uploading a skill, adding custom instructions, supplying a token, selecting a tenant, or explicitly invoking bootstrap.

#### Scenario: Valid invitee installs from a clean client

- **WHEN** a valid invitee installs a promoted package in a clean supported client and chooses its native connect action
- **THEN** the client opens one Exomem login/authorization journey and returns connected after successful admission
- **AND** no setup field other than the Exomem authentication interaction is required

#### Scenario: Production package is inspected for local setup

- **WHEN** the rendered production archive and manifest are inspected
- **THEN** they contain no stdio command, `uvx` dependency, `EXOMEM_VAULT_PATH`, local vault path, setup script, user-editable server URL, or instruction to configure the knowledge base manually

#### Scenario: User starts a fresh chat after authorization

- **WHEN** the authorized user opens a fresh conversation without running a bootstrap command
- **THEN** the plugin's tools and Hosted skills are available through normal host discovery
- **AND** the user is not required to mention or address Exomem to activate relevant behavior

### Requirement: Hosted Skills Teach Automatic Governed Memory

The package SHALL include a generic Hosted core skill and compatible workflow skills that instruct the host to retrieve Exomem context when a turn concerns prior projects, notes, decisions, evidence, failures, or reusable conclusions; cite useful retrieved notes; and preserve newly durable outcomes without dumping transcripts. The instructions SHALL reserve client-native memory for preferences, routing, and immediate working context and SHALL treat Exomem as the long-term governed store.

#### Scenario: Fresh chat asks about seeded project knowledge

- **WHEN** an authorized user asks an ordinary question whose answer depends on seeded Exomem project knowledge without naming Exomem
- **THEN** the host selects the plugin, performs a content-bearing recall, and answers using the relevant memory
- **AND** the answer identifies or cites the useful Exomem note according to the host's supported citation form

#### Scenario: Conversation reaches a durable conclusion

- **WHEN** a normal conversation produces a clear reusable decision, solved problem, failure, pattern, or research finding
- **THEN** the host uses an available governed write route to preserve the distilled outcome without waiting for the phrase "save this" or "use Exomem"
- **AND** it does not store the raw conversation as a compiled note

#### Scenario: Conversation contains no durable or relevant memory

- **WHEN** a turn is trivial, speculative, unrelated to stored context, or would create a redundant write
- **THEN** the skill does not require an Exomem read or write merely to demonstrate plugin activity

#### Scenario: Host mandates approval for a write

- **WHEN** a supported host requires native confirmation before invoking a mutating MCP tool
- **THEN** that native approval may be shown without asking the user to formulate an Exomem-specific prompt
- **AND** the package does not bypass the host's approval policy

### Requirement: Every Hosted Skill Is Executable On The Pinned Profile

Every bundled Hosted skill SHALL declare its exact required Exomem command set. Package validation MUST extract callable Exomem references from the complete skill content and require both declared and extracted commands to be subsets of `hosted-alpha-agent-v1`; it MUST also require every declared command to be used or explicitly justified. The initial package MUST omit workflows whose promised behavior depends on transfer, media, adoption, broad page editing/replacement, maintenance, schema administration, coordination internals, or Tier-2 commands.

#### Scenario: Initial Hosted workflow set is validated

- **WHEN** the core, capture, continue, reflect, research, and review Hosted skills are packaged
- **THEN** every declared and referenced Exomem command belongs to the exact alpha profile
- **AND** each workflow remains complete for the behavior named by its description

#### Scenario: Skill references an excluded command

- **WHEN** any candidate skill prose, example, frontmatter, or referenced helper names a command outside the profile, including `edit_memory`, `replace_memory`, transfer, media, adoption, maintenance, schema, or Tier-2 operations
- **THEN** package generation fails with the skill, reference, and unavailable command
- **AND** no artifact containing that skill can enter pending promotion

#### Scenario: Profile bootstrap is compared with the package

- **WHEN** the build evaluates `bootstrap` under the package's active profile
- **THEN** every advertised callable command is available to the package and every package command is present in the pinned agent contract
- **AND** their active-capability fingerprints match exactly

### Requirement: Packages Are Tenant-Neutral And Secret-Free

A production package SHALL contain only public product metadata, public documentation/legal URLs, public assets, the canonical Hosted MCP resource, skills, and compatibility identity. It MUST NOT contain credentials, tokens, invite material, private endpoints, tenant/user/cell identifiers, vault paths, private note content, personal scaffold values, or environment-specific secrets.

#### Scenario: Release archive passes sensitive-data inspection

- **WHEN** static release gates scan every rendered file and archive entry
- **THEN** all content conforms to the generic scaffold leak policy and the Hosted package allowlist
- **AND** no tenant-specific or secret-bearing value is present

#### Scenario: Two users install the same artifact

- **WHEN** two eligible users install one byte-identical promoted package
- **THEN** OAuth and the Hosted gateway bind each installation to its authenticated account at runtime
- **AND** neither package carries information that can select or reveal either tenant

### Requirement: Release Identity Detects Contract And Skill Drift

Every package candidate SHALL have an immutable lock binding plugin ID/version, target platform/schema, Hosted MCP resource, profile ID, ordered command-surface fingerprint, full schema-contract digest, canonical-definition digest, aggregate skill-content digest, and final artifact digest. Any mismatch MUST fail generation, validation, installation evidence recording, or promotion before distribution.

#### Scenario: Skill content changes without a release rebuild

- **WHEN** a Hosted skill changes after a candidate lock was generated
- **THEN** validation detects the skill or artifact digest mismatch and rejects the candidate

#### Scenario: Agent surface changes under the same candidate

- **WHEN** the consumed profile contract has a different command fingerprint or schema digest from the package lock
- **THEN** the package cannot be promoted or described as compatible
- **AND** maintainers must create a new versioned profile when membership changed and render a new candidate

#### Scenario: Endpoint changes between channels

- **WHEN** a development or staging endpoint differs from the production release input
- **THEN** its artifact has a distinct non-production channel identity and cannot satisfy the production promotion record

### Requirement: Promotion Requires Real Content-Bearing Client Use

Every rendered platform artifact SHALL begin in a non-public `pending` state. Promotion to `live` SHALL require static validation plus a real clean installation on the target host that proves OAuth connection, exact tool discovery, content-bearing recall, citation, governed durable capture, and recall from a later fresh conversation. A manifest validator, successful OAuth callback, MCP initialization, `tools/list`, bootstrap, frontmatter-only read, or mocked client SHALL NOT independently satisfy promotion.

#### Scenario: Static validation passes but native install fails

- **WHEN** a candidate passes repository tests but the supported host rejects or cannot install it
- **THEN** its platform record remains pending or failed
- **AND** no invite or Home surface labels it ready for that platform

#### Scenario: Discovery works but note content is blocked

- **WHEN** the installed client can initialize, list tools, bootstrap, or read metadata but cannot return seeded note content
- **THEN** the content-bearing gate fails and the artifact is not promoted

#### Scenario: Full live journey succeeds

- **WHEN** a clean client completes the required install, authorization, unprompted seeded recall, citation, durable capture, and fresh-chat recall journey
- **THEN** maintainers may register the exact evidence-bound artifact as live for that platform

#### Scenario: Only one platform passes

- **WHEN** the Claude and OpenAI artifacts do not both pass against the same compatibility identity
- **THEN** only the passing platform may be distributed as live
- **AND** the release MUST NOT be called cross-client ready

### Requirement: Paired Acceptance Proves One Tenant Across Clients

The release acceptance suite SHALL share a versioned run with `add-exomem-hosted-mcp-oauth` and SHALL prove the exact product target: "A valid invitee installs one plugin, logs into Exomem once, and can use governed long-term memory automatically from a fresh chat. No configuration or Exomem-specific prompting is required." It MUST also prove that separately authorizing the other supported client for the same Exomem identity attaches to the existing tenant and does not provision another cell or volume.

#### Scenario: Invited first-time user completes the happy path

- **WHEN** a clean invited identity installs a promoted package, completes one uninterrupted Exomem authorization, and begins a fresh chat
- **THEN** the user reaches seeded content and durable capture/recall without manual configuration, a bootstrap instruction, `@Exomem`, or an explicit memory command
- **AND** the paired control-plane evidence contains exactly one identity, tenant, entitlement, logical provisioning operation, cell, and volume

#### Scenario: Same user connects the other supported client

- **WHEN** the same Exomem identity authorizes the other platform's promoted package
- **THEN** that client reaches the same seeded and newly captured memory
- **AND** no second tenant, cell, volume, or first-login entitlement is created

#### Scenario: Failure and isolation matrix is exercised

- **WHEN** acceptance injects duplicate callbacks, concurrent first login, expired or replayed invite, delayed or failed provisioning, exhausted capacity, stale discovery, cell identity mismatch, token rotation or replay, revocation, suspension, deletion, and concurrent two-tenant sentinels
- **THEN** every path matches the paired stable error and retry contract, fails closed without tenant fallback, and leaks no other tenant content

### Requirement: Friends Distribution Uses Only Promoted Tenant-Neutral Artifacts

Private or unlisted friends-cohort distribution SHALL expose install actions only for platform artifacts in `live` state. Eligibility and tenant binding MUST be enforced at OAuth authorization rather than by personalized archives, embedded invite credentials, or manually configured connector URLs. Public directory submission MUST reuse a validated compatibility identity and SHALL require a new promotion when its platform packaging or behavior changes.

#### Scenario: Invited friend opens the install surface

- **WHEN** an eligible user opens the invite or Exomem Home install surface
- **THEN** it presents native install actions only for currently live platform artifacts
- **AND** the downloaded or installed artifact is the same tenant-neutral release used by the cohort

#### Scenario: Artifact is demoted after a regression

- **WHEN** a live client package no longer passes installation, content, or automatic-use verification
- **THEN** its install action is withdrawn or marked unavailable without deleting existing tenant data
- **AND** a corrected artifact must pass a new live promotion before distribution resumes
