## ADDED Requirements

### Requirement: Current platform package contract
The system SHALL render the Hosted OpenAI candidate with its registered package application identity and current connection schema while keeping any provider-issued directory identity outside the existing deterministic package and compatibility pipeline.

#### Scenario: Registered OpenAI package identity renders
- **WHEN** an operator supplies a valid registered `plugin_asdk_app_*` technical identifier
- **THEN** the generated OpenAI app configuration and package locks bind that exact identifier to the canonical Hosted endpoint
- **AND** ChatGPT and Codex are represented as acceptance surfaces of one universal OpenAI plugin

#### Scenario: Malformed package identity is rejected
- **WHEN** an operator supplies a placeholder, malformed, legacy bare `asdk_app_*`, or differently registered package identifier
- **THEN** validation fails before a candidate, promotion, or submission packet can be accepted

#### Scenario: Provider issues a directory identity
- **WHEN** the OpenAI portal assigns a directory identity distinct from the registered package technical ID
- **THEN** the system records it only on the exact directory submission revision and active publication
- **AND** it does not change `.app.json`, package locks, compatibility identity, or prior promotion evidence

#### Scenario: Draft packet precedes directory identity
- **WHEN** an operator renders an OpenAI draft packet before portal submission
- **THEN** a directory identity is not required
- **AND** the packet still binds the registered `plugin_asdk_app_*` technical ID and endpoint

### Requirement: Canonical marketplace definition
The system SHALL maintain one canonical public marketplace definition with provider-specific overlays for Claude Connector, Claude Plugin, and OpenAI Plugin, while importing identity-bearing values from the Hosted runtime definition.

#### Scenario: Provider packets render deterministically
- **WHEN** the same canonical definition, runtime definition, review cases, and accepted artifact inputs are rendered twice
- **THEN** each provider packet and its digest are byte-for-byte identical

#### Scenario: Official-form metadata is complete
- **WHEN** a Claude or OpenAI directory packet is rendered
- **THEN** it carries the checked-in brand asset path and SHA-256, canonical `Productivity` category, documentation and setup URLs, concise use cases, and read/write capability metadata
- **AND** the brand digest is verified against the tracked asset before rendering
- **AND** Claude packet validation enforces current name, tagline, and description limits

#### Scenario: Public metadata drifts from runtime identity
- **WHEN** a packet names or targets a product, publisher, endpoint, package, or registered application inconsistent with its bound runtime definition
- **THEN** marketplace validation fails with the inconsistent field identified

#### Scenario: Editorial copy changes
- **WHEN** listing-only copy changes without changing an identity-bearing runtime field
- **THEN** the listing digest changes
- **AND** the runtime compatibility identity does not change

### Requirement: Complete provider review material
The system SHALL validate provider-ready public copy, support and policy links, regions, release notes, starter prompts, complete tool names/descriptions/input-output structures/annotations, and review cases before reporting a channel submittable.

#### Scenario: OpenAI review cases are complete
- **WHEN** the OpenAI packet is validated
- **THEN** it contains at least five positive cases and three negative cases with expected tool selection and content-safe outcomes

#### Scenario: Claude review prompts are complete
- **WHEN** either Claude channel packet is validated
- **THEN** it contains at least three representative prompts and the required connector or plugin metadata

#### Scenario: Tool semantics are incomplete
- **WHEN** any exposed tool lacks its exact name, title, description, input schema, output structure, retry semantics, or read-only, destructive, and open-world annotations
- **THEN** validation fails before submission readiness

#### Scenario: Non-UI integration has no screenshots
- **WHEN** the Hosted integration exposes no MCP App user interface
- **THEN** its packet declares screenshots not applicable
- **AND** validation does not require fabricated screenshots

### Requirement: Governed automatic-memory claims
The system SHALL describe Exomem as a governed external knowledge store and SHALL NOT claim access to native assistant memory, arbitrary chat history, or content outside the user-authorized Hosted store.

#### Scenario: Automatic recall and capture copy is validated
- **WHEN** listing copy or a starter prompt describes automatic recall or capture
- **THEN** it scopes that behavior to enabled tools, bundled skills, and the user's governed Exomem store
- **AND** review material includes unrelated-query and explicit do-not-capture cases

### Requirement: Secret-free deterministic artifacts
The system SHALL use schemas that prohibit private-value fields and keep provider credentials, raw reviewer identities, tenant IDs, invite links, raw domain-challenge values, and other operator secrets out of definitions, packets, fixtures, receipts, logs, and generated archives.

#### Scenario: Private material enters a tracked input
- **WHEN** validation detects a high-confidence credential, forbidden private-value field, tenant binding, invite token, raw challenge field, or prohibited local path in the marketplace tree
- **THEN** rendering and readiness fail with a redacted diagnostic

#### Scenario: Private field name has a non-string value
- **WHEN** a prohibited private-value field name carries an object, number, null, or other non-string JSON value
- **THEN** validation rejects the field name before rendering, independent of its value type

#### Scenario: Operator-only inputs are required
- **WHEN** a provider action needs credentials or a seeded reviewer account
- **THEN** the packet names the prerequisite and external secret-manager reference class without containing its value

### Requirement: Evidence-based public readiness
The system SHALL derive channel readiness from static validation, versioned and operator-signed live evidence, signed public-admission evidence, and the exact existing live promotion rather than from a mutable readiness assertion.

#### Scenario: Public surface is unhealthy
- **WHEN** signed evidence for legal, support, or documentation content; deployment identity; OAuth discovery; Origin handling; authorization challenge; MCP initialization; tool discovery; or response minimization is missing, expired, mismatched, or unsuccessful
- **THEN** every dependent channel remains blocked with the failed probe reported
- **AND** the signed production evidence binds the supplied trusted deployment SHA-256, current compatibility, command-surface, schema-contract, normalized full-tool-contract digests, Origin rejection, and response-minimization probes

#### Scenario: Exact platform promotion is pending or stale
- **WHEN** the corresponding Claude or OpenAI promotion is not live for the packet's exact compatibility and package or archive identity
- **THEN** the channel cannot be recorded as submitted, in review, or published

#### Scenario: Operator prerequisites are incomplete
- **WHEN** signed evidence for publisher verification, policy approval, domain verification, reviewer-account seeding, or provider registration remains incomplete or invalid
- **THEN** the affected packet may render as a draft
- **AND** it is not reported submittable

#### Scenario: Evidence is authorable but untrusted
- **WHEN** a probe, prerequisite, receipt, or post-install record is unsigned, has an unsupported schema, exceeds its TTL, or does not bind the exact deployment and artifacts
- **THEN** it cannot advance readiness or public status
- **AND** each evidence shape rejects extra fields, future `checked_at` values, and deployment SHA mismatches

### Requirement: Public admission and cost readiness
The system SHALL block public marketplace submission until a signed attestation proves that an ordinary eligible user can acquire access safely and that hosted capacity, quota, abuse, spend, support, and pricing controls are ready.

#### Scenario: Reviewer-only or invite-only access
- **WHEN** only seeded reviewers or private invitees can acquire a functioning Hosted account
- **THEN** draft packets may be generated
- **AND** no public channel is reported submittable

#### Scenario: Public admission is attested
- **WHEN** signed evidence binds ordinary account acquisition, provisioning capacity, per-user quotas, abuse controls, spend alarms, support coverage, and the final public pricing decision to the deployment
- **THEN** the admission component of submission readiness passes

#### Scenario: OpenAI plugin interaction offers a subscription sale
- **WHEN** an OpenAI packet, prompt, or tool output attempts to sell or upsell a digital subscription inside the plugin interaction
- **THEN** marketplace validation fails

#### Scenario: OpenAI packet contains sale language outside listing copy
- **WHEN** buy, Pro, subscribe, upgrade, checkout, or equivalent subscription-sale language appears in an OpenAI tool contract, schema, positive case, or negative case
- **THEN** the complete rendered packet fails validation
- **AND** signed production and post-install evidence attest that sampled public output is sale-free

### Requirement: Independent revisioned marketplace publication state
The system SHALL track append-only Claude Connector, Claude public Plugin, and OpenAI universal Plugin submission revisions independently from runtime artifact promotion and SHALL maintain a separate active-publication pointer per channel.

#### Scenario: Partial public availability
- **WHEN** one channel is published and the other channels are draft, rejected, or withdrawn
- **THEN** status reports only the published channel as public
- **AND** it does not claim cross-client public availability

#### Scenario: Publication receipt is recorded
- **WHEN** an operator records a submitted, in-review, approved, or published provider receipt
- **THEN** the signed append-only record binds the exact channel, state, version, listing digest, compatibility identity, exact promotion-record digest, applicable package and archive locks, provider identity hash, public HTTPS URL, trusted deployment SHA, and fresh canonical timestamp
- **AND** a published OpenAI record requires both the registered `plugin_asdk_app_*` package binding and any distinct directory identity issued by the provider
- **AND** its `provider_directory_id_sha256` equals the lowercase SHA-256 of the trimmed raw UTF-8 provider-issued directory identity

#### Scenario: An update is reviewed while a prior revision remains public
- **WHEN** a new listing revision is submitted while an older revision is the active publication
- **THEN** status keeps the older revision public
- **AND** reports the newer revision independently as pending review

#### Scenario: Provider publication is activated
- **WHEN** a provider has marked an exact revision published
- **THEN** the revision remains non-public until a separately signed fresh non-reviewer post-install proof binds that submission digest, listing digest, package lock, public URL, and deployment SHA
- **AND** an explicit CAS activation moves the pointer only after that proof passes

#### Scenario: Interruption occurs during append, activation, or withdrawal
- **WHEN** a process stops after writing an append-only revision or after moving or clearing a pointer
- **THEN** retrying the same exact operation is idempotent and never duplicates a revision or overwrites a different active pointer
- **AND** status fails closed if an active pointer is stale relative to the head of its listing version

#### Scenario: Concurrent or stale publication update
- **WHEN** a submission transition or active-pointer move is based on stale compare-and-swap state or conflicts with another update
- **THEN** it is rejected without overwriting an append-only record or the active pointer
- **AND** the CLI accepts `--expected-active-submission-sha256 none` as an explicit null-pointer CAS assertion for both record and activation operations

#### Scenario: Listing is rejected or withdrawn
- **WHEN** a provider rejects or an operator withdraws a channel
- **THEN** that exact revision is not public and the active pointer is cleared only when it targets the withdrawn revision
- **AND** its package promotion and hosted tenant data remain unchanged unless a separate runtime or security failure requires demotion

### Requirement: Runtime incidents invalidate public readiness
The system SHALL fail closed when an already published channel no longer has a healthy public surface or exact live package promotion.

#### Scenario: Published runtime or security regression
- **WHEN** OAuth, MCP behavior, privacy, tenant isolation, revocation, or an exact artifact binding fails after publication
- **THEN** status marks the channel non-ready
- **AND** the operator runbook requires package demotion where applicable and immediate withdrawal of the exact active revision

### Requirement: Install-first hosted journey
The system SHALL document and generate onboarding that leads with installing or connecting the official directory entry, completing one OAuth sign-in, and then using Exomem through its bundled tools and skills.

#### Scenario: Supported client onboarding
- **WHEN** a user follows the Claude, ChatGPT, or Codex hosted instructions
- **THEN** the primary path is directory install or connection followed by one Hosted login
- **AND** no vault path, manual MCP JSON editing, API token copy, or repeated explicit `use Exomem` invocation is required

#### Scenario: Client does not activate bundled skills
- **WHEN** a chat surface supports the remote MCP connector but does not automatically activate bundled skills
- **THEN** the documentation provides concise global custom instructions as a labelled fallback
- **AND** the fallback does not alter the Hosted runtime or stored data contract

#### Scenario: Client surface capabilities differ
- **WHEN** onboarding describes Claude or OpenAI support
- **THEN** it distinguishes connector coverage from bundled-skill coverage for each client surface
- **AND** it does not imply that a Claude Code or Cowork plugin enforces skill behavior in Claude.ai

### Requirement: Platform-correct publication channels
The system SHALL model Claude Connector Directory, Claude community/public Plugin distribution, and the OpenAI universal Plugin Directory as three distinct public channels with their current provider submission requirements.

#### Scenario: Claude channels are prepared
- **WHEN** Claude public artifacts are rendered
- **THEN** the connector packet targets the production remote MCP endpoint
- **AND** the plugin packet targets an exact public GitHub commit, passes strict Claude plugin validation, and reuses that connector rather than defining a second Hosted service

#### Scenario: OpenAI channel is prepared
- **WHEN** the OpenAI public artifact is rendered
- **THEN** one packet covers both ChatGPT and Codex and binds the registered application, skills, remote MCP endpoint, prompts, annotations, and review cases

### Requirement: Operator-controlled submission and verification
The system SHALL produce auditable submission packets and runbooks but SHALL leave provider portal actions to an authorized operator and require real directory-install acceptance before claiming publication complete.

#### Scenario: Packet passes local checks
- **WHEN** a deterministic packet passes every local validation
- **THEN** no provider state changes automatically
- **AND** status identifies the remaining operator and live-evidence steps

#### Scenario: Fresh directory installation passes
- **WHEN** a channel is approved and installed through its actual public directory by a fresh non-reviewer account
- **THEN** acceptance verifies OAuth, tool and skill discovery, unprompted governed recall, cited retrieval, durable capture, fresh-chat recall, a do-not-capture case, and revocation
- **AND** the publication record binds that evidence to the exact public URL and artifact identities
