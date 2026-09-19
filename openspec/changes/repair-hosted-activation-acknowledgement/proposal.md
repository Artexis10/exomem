## Why

The first governed write on a newly provisioned hosted cell commits the catalog and then fails because its activation acknowledgement tries to replace read-only custody. This leaves the active store ahead of external authority and blocks content serving; making the local copy writable would still lose the advance on Secret refresh or pod replacement.

## What Changes

- Add a narrowly authenticated acknowledgement protocol for an exact committed activation successor, using the existing provisioner-owned authorization Secret and revision CAS as durable authority.
- Keep the runtime custody mount read-only and the native custody publisher as its only writer. Deliver acknowledged authority promptly rather than waiting for Kubernetes Secret projection after every write.
- Bind exact mutation recovery evidence to the canonical publication before external acknowledgement can fail; split prepared outcomes from effect execution and terminal rendering at shared mutator boundaries.
- Recover the same publication and mutation identity across lost responses, renewal races, custody refresh and pod replacement. Preserve exact external/store parity and all non-activation authority fields.
- Refuse unsupported acknowledgement wiring before canonical mutation; retain explicit recoverable state if acknowledgement is interrupted after commit.
- Prove the real mounted-image path, including capture, cited recall, refresh and restart, before owner launch. This is a prerequisite repair within the existing owner-first launch sequence.

## Capabilities

### New Capabilities

- `hosted-activation-acknowledgement`: Authenticated committed-publication proof, external successor CAS, prompt custody delivery, bounded retries and same-publication recovery.

### Modified Capabilities

- `governance-kernel`: Hosted publication uses external acknowledgement authority while preserving exact active-tuple parity and committed-publication recovery.
- `hosted-mutation-safety`: Exact same-operation terminal recovery across the canonical-commit/external-acknowledgement cut for every supported hosted mutator.
- `authorization-session-binding`: Hosted activation updates and renewal share one monotonic authoritative bundle; refresh and restart cannot restore an older activation tuple.

## Impact

Runtime governance publication/custody, private control transport, the native custody publisher, provisioner authorization-bundle CAS and authentication, cell/platform charts and admission policy, release/deployment identity, and connected acceptance tests. Standalone custody behavior is preserved. No model, optional enrichment feature, public self-service, friend invitation, or general gateway redesign is introduced.
