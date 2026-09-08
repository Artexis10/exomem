## Context

See proposal.md for motivation. The server subclasses FastMCP to insert raw
authorization-carrier redaction and run sanitized stdio streams before SDK
logging. Its stateless HTTP route also adds GET for legacy connector clients.
The OAuth proxy retains durable encrypted storage and stable signing keys.
FastMCP 4 moves to MCP SDK v2, snake-case Python protocol fields and httpx2;
these are integration boundaries, not canonical-storage changes.

## Goals / Non-Goals

Goals: retain existing command, authentication and credential-hygiene contracts
on both legacy and modern transports; pin one supported framework version.

Non-goals: change identity/issuer configuration, invalidate sessions, introduce
server-side sampling or task orchestration, alter writer ordering, optimize
indexes, or infer production stability from a dependency upgrade.

## Decisions

1. Upgrade the exact dependency pin and minimally regenerate the lock with the
   repository-pinned uv. Use the actual installed SDK source and failing tests
   to adapt imports, typed fields, exception constructors and transport calls.
   Avoid a dual-version abstraction: one lock and one runtime are supported.
2. Retain the raw HTTP/stdio security adapters. Remove only the GET route patch
   if real legacy-client tests establish that upstream now handles it. Modern
   discovery never establishes governance authority: tool-call middleware must
   bind and verify every request independently.
3. Match provider-client exception handling to its actual HTTP library. Retain
   unrelated httpx clients unless their upstream contract requires migration.
   Keep JWT derivation salts, issuer, storage identifiers and refresh lifetimes
   unchanged; test old-token storage compatibility and failure sanitization.
4. Exercise native snake-case protocol fields with the compatibility bridge
   disabled. Test wire aliases separately; JSON-RPC wire spelling must not
   change alongside Python API spelling.
   Preserve the v1 discovery fingerprint encoding by normalizing only the
   top-level field order and `_meta` alias; unexpected fields still refuse and
   nested schema order and values remain load-bearing.
   Enable the supported strict-input option: the v4 lax default explicitly
   overrides field-level strict annotations and otherwise admits booleans as
   bounded integers. Published JSON input types, not coercible strings or
   booleans, define accepted tool arguments.
5. Establish a FastMCP 3 baseline first, then run scoped auth/transport tests on
   4 and extend public tool, hosted mounting, ledger and workflow coverage.
   Complete independent security review, fresh verification and the full
   completion-boundary suite before delivery. No speed claim without a paired
   measurement using the same fixture and transport.

## Risks / Trade-offs

- SDK request metadata or dispatch changes could bypass carrier custody → real
  stdio and HTTP wire tests, malformed/duplicate credential cases, and an
  independent security review of the final diff.
- Internal OAuth APIs could change persisted sessions → existing callback,
  refresh, revocation, migration and shared-storage tests plus provider errors.
- Modern sessionless transport differs from initialized clients → exercise both
  explicitly, including authenticated GET compatibility and reconnects.
- Broad dependency regressions → preserve the baseline, keep upgrade changes
  scoped, run the full completion boundary and retain the previous release for
  rollback instead of editing live state.

## Migration Plan

Deliver through the normal ready PR and release gates. No canonical migration
or configuration rewrite is required. A rollout must install the released
wheel with the service's existing extras and verify health and authenticated
MCP behavior. Roll back to the prior wheel if acceptance fails; never delete
or regenerate authentication state to make the new runtime work.
