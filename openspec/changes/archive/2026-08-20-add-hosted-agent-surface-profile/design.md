## Context

Hosted cells already expose a deterministic, authenticated private REST command contract generated from `PRODUCT_COMMANDS`. That contract intentionally includes control-plane and broad product operations needed by Home, transfers, adoption, and maintenance. The planned shared Substrate MCP facade needs a smaller public-agent view, but placing a second allowlist in Substrate would violate the registry-as-source-of-truth rule and allow MCP discovery, bootstrap guidance, and the cell contract to drift.

The existing `ActiveSurfaceDescriptor` and bootstrap filtering solve part of the problem: when an adapter binds an exact command set, `bootstrap` removes unavailable recommendations. A contract-only profile is not sufficient, however, because the current private command route always binds the full `private-command-router` descriptor. This change therefore adds a separate authenticated agent contract/dispatch path while leaving the current private contract and command route unchanged.

## Goals / Non-Goals

**Goals:**

- Define one stable `hosted-alpha-agent-v1` profile inside the product registry.
- Expose only the governed text-memory operations needed for private-alpha capture, recall, review, and linking.
- Produce a deterministic profile contract containing canonical schemas plus an active-capability fingerprint.
- Enforce the profile at an authenticated cell route, including for `bootstrap`.
- Prove bootstrap guidance cannot advertise a command outside the profile.
- Preserve the existing private Hosted contract, digest, command routing, and release fixture by default.

**Non-Goals:**

- Public MCP transport, OAuth, token scopes, tenant routing, revocation, or Substrate UI.
- Client skill/plugin packaging or the existing ChatGPT connector promotion contract.
- Provider infrastructure, cell deployment, release manifests, or live rollout.
- Transfers, uploads, adoption, media, maintenance/schema administration, Tier-2 tools, or general command-surface redesign.

## Decisions

### 1. Profile membership is one set-level registry policy, not transport policy

An immutable `ProductSurfaceProfile` registry lives beside `PRODUCT_COMMANDS`. Each profile owns one ordered command-name tuple and resolves those names to canonical `Command` objects while rejecting missing, duplicate, non-REST, or non-Tier-1 entries. `product_commands_for_profile(profile, surface)` rejects an unknown profile and returns canonical commands in the profile's pinned order. Command schema, description, read/write classification, annotations, and execution still come exclusively from `PRODUCT_COMMANDS`; only set-level exposure policy lives in the profile registry.

An allowlist in `hosted_gateway.py` was rejected because transport code must not own product exposure policy. Scattering versioned membership across each `Command` was rejected because exact profile membership is a set-level decision. Inferring membership from tier or `product_surface` was rejected because those fields are broader product taxonomy, not a security/exposure decision.

### 2. The v1 profile is deliberately narrow

The exact ordered profile is:

1. `bootstrap`
2. `ask_memory`
3. `read_memory`
4. `browse_memory`
5. `remember`
6. `observe_memory`
7. `capture_source`
8. `compile_source`
9. `preserve_evidence`
10. `review_memory`
11. `review_item_context`
12. `triage_memory`
13. `connect_memory`

This includes the governed text-memory loop while excluding coordination internals, transfer, media processing, both adoption commands, maintenance/schema administration, and all Tier-2 commands. It also excludes `edit_memory` and `replace_memory`: both are intentionally broad page-level primitives that can target instruction/schema material, so they are not appropriate for a first Hosted agent boundary. Governed semantic-unit updates remain available through `observe_memory`. `v1` is immutable: any membership change creates a new profile identifier and contract.

### 3. The agent contract is an additive derived variant

`build_gateway_contract()` remains the full private control-plane contract with its existing signature and output. A separate `build_agent_gateway_contract(profile=...)` selects the profile registry, reuses the existing envelope/compatibility fields except the unrelated transfer-grant capability, and adds profile metadata from an `ActiveSurfaceDescriptor`. Each agent command also carries the canonical MCP discovery description, input JSON Schema, and annotations generated from the same bound `Command` definition used by FastMCP. This prevents the current `/private/exomem/v1/contract` response and digest from changing while giving Substrate sufficient data for `tools/list`.

The separate builder avoids changing the legacy contract. The cell additionally exposes authenticated profile-specific private endpoints; the public MCP transport and OAuth boundary still belong to Substrate.

### 4. Profile enforcement happens at a distinct authenticated cell path

The cell adds private agent-contract and agent-command routes scoped to the immutable profile. They use the same service authentication and trusted cell/principal context as existing private routes, but resolve commands only from the profile and bind the profile descriptor during invocation. Excluded commands fail with `COMMAND_NOT_FOUND` before a leaf or lifecycle admission runs. The existing full private contract/command routes remain unchanged for Home and control-plane workflows.

The public caller never selects a profile through body, query, cookie, or untrusted header. The shared Substrate gateway pins the private route for its token policy and constructs the private request after resolving the authenticated account. A distinct public token audience/scope is part of the paired Substrate OAuth design; the cell continues to trust only its private service credential plus trusted routing context.

### 5. One descriptor links contract discovery to bootstrap behavior

`hosted_agent_surface_descriptor(profile)` is the sole constructor for the active Hosted agent descriptor. The derived contract embeds its metadata and fingerprint, and the agent-command route binds it around every invocation. A forwarded `bootstrap` therefore executes inside the cell with the exact descriptor, causing the existing recursive bootstrap filter to remove all unavailable tools, routes, and workflow guidance.

Tests compare the contract command names, descriptor command names, fingerprint, and every callable reference extracted from all bootstrap profiles. This is the drift gate needed by both Claude and ChatGPT.

### 6. Verification separates pure contracts from route integration

Pure tests exercise registry, contract, descriptor, MCP schema, and bootstrap behavior without constructing POSIX-oriented runtime/temp directories. One focused ASGI integration test injects the existing test seams for runtime-temp authority, exercises the new authenticated routes, proves excluded commands never invoke a leaf, proves `bootstrap` reports the exact profile, and confirms the legacy route remains full. The untouched broad hosted-route baseline still runs in Linux/CI; it currently fails before route registration on Windows due to `os.geteuid` and Unix mode-bit assumptions.

## Risks / Trade-offs

- [Profile becomes stale as the product registry evolves] → Exact ordered membership and explicit forbidden-command tests fail on intentional or accidental change.
- [Derived contract diverges from the full command schema] → Both builders share the same command serializer; tests compare every shared command entry for equality.
- [Public MCP discovery loses validation or safety metadata] → Generate canonical FastMCP input schema and annotations from the same bound command functions and fingerprint the full agent contract.
- [An agent rewrites governance or instruction files] → Keep arbitrary-page `edit_memory` and `replace_memory` outside v1; expose only constrained semantic-unit mutation through `observe_memory`.
- [Bootstrap advertises a filtered-out route through nested guidance] → Reuse the active-surface filter and extract callable references from compact, full, and diagnostics payloads.
- [A future gateway accidentally consumes the full contract] → Give the agent variant a stable profile identifier and fingerprint; the Substrate change must pin both.
- [The alpha needs media or adoption sooner] → Add a new versioned profile; never revise `hosted-alpha-agent-v1` membership.

## Migration Plan

1. Land the additive profile metadata, builders, and tests with no runtime consumer.
2. In the separate Substrate connector change, pin `hosted-alpha-agent-v1`, its membership fingerprint, and its full schema-contract digest while generating MCP discovery and forwarding policy.
3. Forward agent calls only to the profile-specific private cell path; the cell binds the matching descriptor for `bootstrap` and every other command.
4. Roll back by removing the consumer; the existing full private contract remains available and unchanged throughout.

## Open Questions

None for this Exomem-side slice. OAuth scopes and client-specific installation mechanics remain in the Substrate connector design.
