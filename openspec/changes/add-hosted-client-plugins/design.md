## Context

Exomem already ships a generated Claude Code plugin for local stdio use, and the completed `add-hosted-agent-surface-profile` change defines the immutable, thirteen-command `hosted-alpha-agent-v1` private agent contract. Neither is the Hosted product experience. The local plugin assumes `uvx`, `EXOMEM_VAULT_PATH`, hooks, and the broad local skill scaffold; the Hosted product needs remote MCP, Exomem-owned OAuth, no local runtime, and skills that mention only the alpha profile.

The paired Substrate change `add-exomem-hosted-mcp-oauth` owns the public MCP resource, OAuth lifecycle, invite admission, tenant provisioning, and authenticated routing. This repository owns what the clients install and what the assistant learns about when and how to use Exomem. The package must work in chat surfaces where hooks are unavailable, so automatic behavior must come from client-native plugin discovery, tool descriptions, and skill instructions, then be proven in the real clients.

The release target is exact:

> A valid invitee installs one plugin, logs into Exomem once, and can use governed long-term memory automatically from a fresh chat. No configuration or Exomem-specific prompting is required.

## Goals / Non-Goals

**Goals:**

- Publish one Exomem identity as native Claude and OpenAI plugin artifacts backed by the same canonical definition.
- Require only native installation and one Exomem authorization per client installation.
- Make recall and durable capture happen naturally in relevant fresh conversations without `@Exomem`, a bootstrap prompt, or custom instructions.
- Keep every bundled instruction executable on exactly `hosted-alpha-agent-v1`.
- Make package identity reproducible and bind it to the exact remote tool contract plus the gateway-owned OAuth discovery overlay; live evidence remains outside that immutable identity.
- Treat successful real installation and content-bearing use as release gates, not manual follow-up.
- Start with private or unlisted friends-cohort distribution while preserving a clean path to public directories.

**Non-Goals:**

- Replacing or changing the existing local Claude Code plugin, stdio transport, hooks, or generic vault scaffold.
- Adding transfer, media, adoption, broad page editing/replacement, maintenance, schema administration, or Tier-2 tools to the alpha profile.
- Implementing OAuth, tenant provisioning, billing, public MCP transport, or cell routing in this repository.
- Guaranteeing invocation on a host or account plan that does not support the required plugin, skill, MCP, or OAuth capabilities.
- Adding a server-side reasoning model. The host model reasons; Exomem remains governed storage, retrieval, and deterministic substrate.

## Decisions

### 1. One canonical Hosted definition renders two platform packages

A new Hosted plugin source owns the product identity, production MCP resource URL, semantic plugin version, `hosted-alpha-agent-v1` binding, package policy, shared assets, and Hosted skill set. Deterministic renderers produce:

- a Claude package using the currently supported Claude plugin/connector manifest and bundled skills; and
- an OpenAI package with `.codex-plugin/plugin.json`, `.mcp.json`, required `.app.json` registered-app mapping, `skills/`, and assets, plus marketplace metadata whose authentication policy is `ON_INSTALL`.

The generated artifacts are checked and validated, but the canonical definition and Hosted skill sources are hand-authored. Platform adapters may change syntax without creating independent product behavior. A generic zip with setup prose was rejected because it recreates the configuration burden this change exists to remove. Hand-maintaining two unrelated packages was rejected because endpoint, skills, and contract identity would drift.

The published MCP URL is a release input selected by maintainers and rendered as one literal HTTPS endpoint: `https://substratesystems.io/api/exomem/mcp/v1`. It is never a user input. Development endpoints may be rendered only into non-distributable fixtures with an explicit development channel. The registered OpenAI app ID is a separate operator release input; discovering a developer app proves package shape only, not supported distribution.

### 2. The initial Hosted skill set is intentionally smaller than the local scaffold

The initial bundle contains one Hosted core skill plus Hosted variants of:

1. `exomem-capture`
2. `exomem-continue`
3. `exomem-reflect`
4. `exomem-research`
5. `exomem-review`

These variants are authored against the alpha profile rather than mechanically deleting lines from the local skills. `exomem-curate` and `exomem-defrag` depend on broad `edit_memory`/`replace_memory`; `exomem-ingest` and `exomem-media` teach transfer or media paths. They remain absent until a future profile and purpose-built Hosted variant make their full promise executable.

Each Hosted skill declares its exact required command set in canonical metadata. Package generation parses callable references from the full skill content and requires both the declaration and every reference to be a subset of the selected profile. It also exercises `bootstrap` under the same profile and rejects unavailable recommendations. Silently stripping unsupported calls was rejected because it can leave plausible but unsafe workflows behind.

### 3. Automatic memory behavior is an explicit skill contract

The core skill teaches the host to:

- retrieve quietly when a turn touches the user's projects, prior notes, decisions, failures, evidence, or reusable conclusions;
- prefer Exomem over client-native memory for durable project/domain knowledge while leaving preferences and immediate working context to the host;
- cite useful retrieved notes in the answer;
- preserve a durable outcome after it becomes clear, using a concise compiled note or observation rather than a transcript dump;
- avoid writes for trivial, speculative, sensitive-without-purpose, or already-current material; and
- use governed source/evidence routes before compiling external claims.

Activation metadata and descriptions use plain intent language rather than requiring the user to name Exomem. Chat packages do not depend on hooks because Claude web chat and ChatGPT/Codex cannot be assumed to expose them. Hooks remain useful for local coding clients but are not part of the Hosted acceptance claim.

Automatic selection is partly host behavior, so static prompt inspection is necessary but insufficient. A release is promoted only after a fresh-chat live test demonstrates unprompted content recall and durable capture/recall on that platform. Host-native approval UI is permitted when the platform mandates it; asking the user to type an Exomem-specific instruction is not.

### 4. Every artifact carries one immutable compatibility identity

The immutable compatibility manifest contains the canonical definition and exact ordered raw tool schemas/annotations, plus the gateway-owned OAuth discovery overlay (per-tool read/write scopes and required runtime `mcp/www_authenticate` metadata). Pending Exomem packages and the paired Substrate contract may reference this one identity. The candidate lock contains at least:

- plugin ID and semantic version;
- target platform and package schema version;
- canonical Hosted MCP resource URL;
- `hosted-alpha-agent-v1` profile ID;
- ordered command-surface fingerprint;
- full schema-contract digest;
- canonical Hosted definition digest;
- aggregate Hosted skill-content digest; and
- artifact digest.

The platform manifest contains only fields accepted by that platform; unsupported compatibility metadata lives in the package lock rather than being smuggled into manifests. Live evidence is deliberately excluded from the compatibility digest so promotion cannot form a dependency cycle. The build fails if the agent contract, skill content, endpoint, generated artifacts, or lock disagree. Any profile membership change requires a new profile identifier and a new plugin candidate; a silent profile change under an existing package is forbidden.

The package never contains an access token, refresh token, invite token, user/tenant/cell identifier, private cell endpoint, service credential, vault path, `EXOMEM_VAULT_PATH`, or local executable command. OAuth state belongs to the client and Substrate, not the archive.

### 5. Promotion is two-phase and platform-specific

Each rendered platform artifact begins in `pending`. Static validation covers manifest schema, referenced files, skill syntax, no-private-leak rules, no placeholders, HTTPS endpoint policy, exact hashes, and profile dependency closure. Pending artifacts may be installed only by maintainers or the test cohort.

Promotion to `live` requires evidence from an actual clean installation in the supported host, not merely a validator or MCP handshake. The evidence record binds platform/client version, plugin version, endpoint, profile fingerprint, contract digest, test identity, timestamp, and redacted result hashes. It also contains an operator-signed `oauth_client_config_sha256`: the deployment computes this lowercase 64-hex value as SHA-256 over the ASCII prefix `exomem-oauth-client-config:v1\0` followed by canonical UTF-8 JSON (sorted keys, no whitespace, `ensure_ascii=false`) for `{platform, admission_mode, client_id, redirect_uris(sorted exact raw strings), token_endpoint_auth_method:'none'}`. This tuple is public; the evidence signature supplies authority. The package validator verifies only the digest shape and signed evidence. This live datum is outside the package lock and compatibility identity. It must prove:

1. native install succeeds;
2. the Exomem authorization journey completes;
3. `initialize` and `tools/list` expose the pinned profile;
4. a seeded-vault question returns note content and a citation;
5. a durable conclusion is captured from an ordinary conversation without an Exomem-specific prompt; and
6. a fresh conversation recalls that conclusion.

This deliberately treats discovery-only success as a failure. The existing personal ChatGPT connector demonstrated that bootstrap and metadata can work while content-bearing reads are blocked, and the Claude plugin history demonstrated that CI-valid manifests can still fail at real installation.

A platform can be withheld independently when its live gate fails. The overall release may be described as cross-client ready only when both Claude and OpenAI records are live against the same compatibility identity.

### 6. Private friends-cohort distribution precedes directory submission

The first release is private or unlisted. Invite email and Exomem Home may expose platform-specific install actions only for eligible identities and only for artifacts whose promotion record is live. The archive itself remains tenant-neutral; eligibility is enforced by OAuth, not by secret download links or package variants.

Public Claude/OpenAI directory submission is a later channel transition using the same validated artifacts and privacy/terms metadata. Friends are the design-partner cohort for onboarding, comprehension, reliability, and habit formation—not proof of broad market demand.

### 7. One paired acceptance matrix is the release authority

The Exomem and Substrate changes share a versioned acceptance fixture and run identifier. For each platform, a clean invited identity installs the package, authorizes once, waits through any normal provisioning state, asks an ordinary seeded-memory question, reaches content, produces a durable conclusion, and recalls it from a fresh conversation without configuration or Exomem-specific wording.

The matrix then authorizes the other supported client as the same Exomem identity and proves it attaches to the same tenant without another cell or volume. Each client legitimately performs its own OAuth authorization; "logs in once" means one uninterrupted Exomem login/authorization journey for an installation, not unsafe token sharing between Claude and OpenAI.

The paired test also covers duplicate callbacks, concurrent first authorization, expired/replayed invites, delayed and terminal provisioning, exhausted capacity, stale contract discovery, cell identity mismatch, token rotation/replay, revocation, suspension, deletion, and two-tenant sentinel isolation. Promotion requires content-bearing reads and writes; `tools/list`, bootstrap, or a successful OAuth callback alone never satisfy it.

## Risks / Trade-offs

- [Host auto-selection changes outside our control] → Pin supported host versions in evidence, keep intent descriptions compact, run fresh-chat live gates for every promoted package, and demote a platform when the behavior regresses.
- [Hosted skills drift from the richer local scaffold] → Keep a deliberately small Hosted source, declare tool dependencies, reuse governed vocabulary fixtures where safe, and test common invariants without auto-stripping local prose.
- [A platform accepts the manifest but rejects installation] → Make real clean installation mandatory and retain the platform-specific pending state until it passes.
- [Discovery succeeds while note content is blocked] → Require a seeded content-bearing read, citation, write, and later recall in the live gate.
- [A stale package invokes a changed surface] → Bind endpoint, profile, fingerprints, schema digest, skills, and artifact into one lock and require exact compatibility before promotion.
- [Automatic capture becomes noisy] → Teach durability thresholds and no-transcript behavior, then evaluate false-positive writes in the friends cohort before public distribution.
- [Private distribution is mistaken for market validation] → Track activation, repeated recall, unwanted-write rate, support burden, and retention separately from friend enthusiasm.

## Migration Plan

1. Land and archive `add-hosted-agent-surface-profile`; record its immutable profile and schema digests.
2. Add the canonical Hosted definition, Hosted skill set, renderers, validators, and pending promotion records without publishing them.
3. Land and deploy the paired Substrate MCP/OAuth change with the same contract identity.
4. Render release candidates, complete static validation, and install them into clean Claude and OpenAI test accounts.
5. Run the paired live matrix, promote passing artifacts, and expose private/unlisted install actions to invited friends.
6. Observe cohort activation and reliability before submitting the unchanged package identity to public directories.

Rollback demotes or unlists the affected platform artifact and revokes its OAuth client admission. Existing tenants and vault data remain intact and accessible through Home or another live client. Rollback does not delete tenant data or restore a broader tool profile.

## Open Questions

None. The production MCP hostname and platform schema versions are explicit release inputs resolved before candidate generation, not user-facing configuration or unresolved architecture.
