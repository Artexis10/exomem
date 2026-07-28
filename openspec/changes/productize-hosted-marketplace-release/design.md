## Context

The native Hosted Exomem packages, OAuth flow, shared hosted identity, deterministic release locks, and private/unlisted promotion state machine already exist under `add-hosted-client-plugins`. They are deliberately fail-closed and are not yet live: both platform promotion records are pending and their remaining acceptance tasks require clean accounts and production evidence.

Public distribution adds a different lifecycle. Claude has separate Connector Directory and Plugin Directory channels, while OpenAI has one universal Plugin Directory shared by ChatGPT and Codex. Each channel needs provider-shaped copy, review cases, public policy/support surfaces, exact bindings to an accepted package, and durable publication evidence. None of those concerns should weaken or overload runtime promotion.

The public Hosted endpoint and policy pages are owned by the paired Substrate deployment. Exomem can validate their declared and observed state, but it cannot make a broken deployment healthy from this repository. Marketplace submission also requires operator-held provider access, a verified publisher, domain proof, and a seeded reviewer account; those secrets and identities must stay outside Git.

## Goals / Non-Goals

**Goals:**

- Produce deterministic, redacted submission packets for Claude Connector, Claude Plugin, and the OpenAI universal Plugin Directory from one canonical public definition.
- Preserve the existing registered OpenAI `asdk_app_*` package identity, align the platform `.mcp.json` shape, and track the later `plugin_asdk_app_*` directory identity outside package locks.
- Fail closed unless public metadata, tool annotations, test cases, production surfaces, and exact live artifact bindings are ready for the selected channel.
- Record directory submission and publication independently from package promotion, including partial public availability and safe withdrawal.
- Make the user journey install, authenticate once, then use Exomem naturally; custom instructions remain a fallback rather than the main onboarding path.

**Non-Goals:**

- Rebuilding the Hosted MCP server, OAuth, tenant provisioning, cell isolation, or package renderer.
- Automating provider portal submission, storing reviewer credentials, or claiming approval before a provider supplies it.
- Marking the existing clean-account and failure-matrix acceptance tasks complete without real evidence.
- Adding speculative screenshots for integrations that have no MCP App UI.
- Coupling editorial listing changes to the runtime compatibility identity.
- Treating a seeded reviewer account or private friends cohort as proof that ordinary public admission, support, or hosted cost controls are ready.
- Selling or upselling digital subscriptions through an OpenAI plugin interaction.

## Decisions

### Keep public listing data separate from runtime identity

`plugins/hosted/definition.json` remains the authority for endpoint and identity-bearing package fields. A new marketplace definition owns common listing copy, channel overlays, prompts, regions, release notes, public URLs, and review-case references. Values that must match the runtime definition are imported and validated, not copied into a second editable source.

This keeps editorial copy outside the compatibility digest while still preventing a listing from naming a different product, publisher, endpoint, or package. The alternative—adding every listing field to `definition.json`—would force package re-promotion for harmless copy edits.

### Generate provider-shaped packets from one canonical definition

The existing renderer will expose deterministic directory validation, rendering, status, and record operations. It will generate separate redacted packets for `claude-connector`, `claude-plugin`, and `openai-plugin`; one generic JSON payload would conceal provider-specific omissions and incorrectly imply that Claude has one publication channel.

The packets contain public form values, artifact/digest bindings, tool metadata, prompts, and review cases. They contain instructions for obtaining operator-only inputs but never credentials, tenant IDs, invite links, raw provider challenge values, or private reviewer identifiers.

### Model package promotion and directory publication as separate revisioned state

The existing `pending -> live|failed` promotion records continue to answer whether an exact client artifact passed runtime acceptance. New directory records answer whether a channel is `draft`, `submitted`, `in_review`, `published`, `rejected`, or `withdrawn`.

Directory submissions are append-only revisions keyed by channel and listing digest/version. Their states are `draft`, `submitted`, `in_review`, `approved`, `published`, `rejected`, or `withdrawn`; `published` is deliberately not activation. Only a separate compare-and-swap active-publication pointer, moved after fresh post-directory-install evidence for that exact revision, makes a listing public. Every status, transition, receipt, activation, and evidence bundle carries the explicit trusted deployment SHA-256. Per-listing-version heads retain a v2 review independently from an active v1; exact withdrawal of v1 never mutates v2. Crash recovery treats an already appended identical revision or already moved pointer as an idempotent retry, and a stale pointer fails closed until a withdrawal retry clears it. A transition beyond `draft` requires the corresponding exact platform promotion to be live. A signed publication receipt binds the channel/state/version, listing digest, compatibility identity, package/archive locks, exact promotion record digest, provider identity hash, HTTPS public URL, trusted deployment SHA, and fresh timestamp. OpenAI hashes both registered and directory IDs as the lowercase SHA-256 of their trimmed raw UTF-8 values; the provider-issued `plugin_asdk_app_*` raw ID and its matching hash are mandatory when published. Listing withdrawal targets one exact revision and never deletes or mutates tenant data. A runtime/security failure invalidates readiness and requires withdrawal, but a copy rejection does not demote a healthy private package.

### Make readiness an evidence bundle, not a boolean assertion

Static validation checks provider limits, public URLs, skills, complete tool names/descriptions/input-output contracts/annotations, test-case counts, identity format, package locks, and leak rules. It rejects forbidden private field names independent of their JSON value type, validates exact state-specific submission records, and scans every user-facing OpenAI packet string—including tool contracts and review cases—for subscription-sale language. Live readiness consumes separately supplied, versioned, redacted, operator-signed evidence for legal/support/documentation content digests, deployment identity, OAuth discovery, Origin handling, MCP authorization challenge, initialization, tool discovery, sampled response minimization, and sampled-output sale-freedom. Evidence has an explicit schema, bounded TTL, and exact artifact/deployment bindings. Submission readiness also verifies signed public-admission and operator-prerequisite evidence plus current promotion/publication bindings.

The validator reports per-channel blockers and derived status. It never turns a user-authored `ready: true` field or an unsigned receipt into evidence. A channel is public only when the active pointer names a verified published revision whose directory URL and post-directory-install evidence still bind the exact listing and artifact. This preserves the existing fail-closed release posture.

### Separate OpenAI package and directory identities

The existing OpenAI package renderer continues to accept the registered `asdk_app_*` identity used by `.app.json`, release locks, and promotion evidence. It emits OpenAI's platform-valid `.mcp.json` connection map while the Claude candidate retains its own `mcpServers` wrapper. The provider may later issue a top-level `plugin_asdk_app_*` directory identity; that value belongs only to the OpenAI submission revision, active-publication pointer, and receipt. A draft submission packet does not require it. ChatGPT and Codex are acceptance surfaces for the same published channel, not separate listings.

### Keep actual provider operations outside the CLI

The CLI produces deterministic packets and validates redacted receipts; a human operator uses the provider portals. Provider roles, publisher verification, reviewer credentials, and domain challenge secrets make headless portal automation brittle and unsafe. The submission runbook states exactly what remains external and how to bind the resulting receipt back to the generated artifact.

### Use an install-first product story

Documentation and generated starter guidance lead with directory install/connection and one OAuth sign-in. A surface matrix distinguishes the Claude Connector (Claude.ai, Desktop, Mobile, Code, and Cowork), the community/public Claude plugin bundle (skills for Code and Cowork), and the universal OpenAI plugin (ChatGPT and Codex). Bundled skills and the remote MCP endpoint provide automatic recall and capture behavior where the client supports them. Global custom instructions are documented only as a one-time fallback for chat surfaces that do not activate bundled skills; they are not represented as a runtime or storage requirement.

## Risks / Trade-offs

- [Provider schemas and limits change] -> Keep channel validation isolated behind canonical provider adapters, test current identifiers and payload shapes, and require regeneration before submission.
- [A public listing drifts from an accepted artifact] -> Bind every receipt to listing, compatibility, promotion, and package/archive digests and fail on any mismatch.
- [Production is healthy during submission but regresses later] -> Make readiness re-runnable, expose per-channel blockers, and document withdrawal plus package demotion for runtime/security incidents.
- [Marketplace reviewers need realistic data] -> Reuse the governed seeded acceptance fixture and keep the reviewer account and identifiers in the external secret manager.
- [A reviewer account passes while public users cannot onboard safely] -> Require signed public-admission evidence for ordinary acquisition, capacity, quotas, abuse controls, spend alarms, support, and pricing after the friends cohort.
- [Automatic-memory copy overclaims client behavior] -> Describe observable recall/capture behavior and include negative/do-not-capture cases; do not claim access to native assistant memory or arbitrary chat history.
- [Legal copy is operationally accurate but not legally approved] -> Gate actual submission on explicit policy approval without embedding an unreviewed approval assertion in Git.
- [Claude publishes one channel before the other] -> Report channel-level state and never derive all-client availability from a partial release.

## Migration Plan

1. Land the paired Substrate policy, onboarding, domain-challenge, OAuth/MCP health, and production-probe changes.
2. Freeze the public identity and registered OpenAI app ID; update and rerender candidates through the existing package pipeline.
3. Generate and validate all three directory packets and reviewer cases.
4. Complete the remaining clean-account, shared-identity, failure-matrix, security-review, and independent-verifier tasks in `add-hosted-client-plugins`.
5. Promote the exact Claude and OpenAI artifacts with the existing signed evidence flow.
6. After the friends cohort, sign public-admission evidence covering ordinary onboarding and hosted cost controls; do not submit while the product remains reviewer-only or invitation-dependent.
7. Submit Claude Connector, Claude community/public Plugin, and OpenAI Plugin independently, recording append-only signed revisions and exact receipts.
8. Run fresh-account post-publication acceptance through each real directory surface before moving its active-publication pointer and claiming that channel public.

Rollback is channel-specific. Withdraw a bad listing and demote an affected package when the cause is runtime, OAuth, privacy, or isolation. Preserve hosted tenants and data; directory withdrawal is not a data-deletion mechanism. Rejected copy returns only the directory record to draft.

## Open Questions

- The registered OpenAI `asdk_app_*` package value exists before rendering; the separately provider-issued `plugin_asdk_app_*` directory value, Claude connector slug, and final public listing URLs remain operator inputs until assigned.
- Verified publisher wording and legal policy text require owner/legal approval before portal submission.
- Live acceptance and publication receipts cannot exist until the paired production deployment is healthy and clean reviewer accounts are available.
