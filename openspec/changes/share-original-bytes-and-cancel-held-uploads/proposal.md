# Proposal

This change is in design. The local storage compatibility choice remains open; implementation is not ready.

## Why

An archive should not store another original merely because identical bytes belong to another governed filing. Failed uploads also need an explicit way to release temporary bytes without discarding retryable uploads automatically.

These capabilities change storage and upload contracts. They need a durable specification that separates physical byte identity from filing identity and cancellation from preservation.

## What Changes

- Share canonical original bytes within one vault while each filing keeps its identity, provenance, permissions, and release grants.
- Preserve existing logical duplicate outcomes, including `already_stored`. Internal storage reuse must not reveal another filing or merge its governance.
- Reuse existing Cloud reference sharing, random object names, pinned versions, encryption, protection records, and retained-backup collection rules.
- Specify local sharing without claiming that filesystem links provide the same guarantee on every supported filesystem.
- **Potentially breaking, awaiting the compatibility choice:** let logical filing paths resolve to a shared local original instead of requiring separate ordinary files.
- Add one explicit operation that cancels an owned, unclaimed held upload, including a hold restored after preservation failed.
- Confirm temporary-byte removal before reporting released capacity. Keep retryable holds unchanged unless cancellation or existing expiry removes them.
- Preserve an active preservation attempt and every canonical Source or Evidence artifact; cancelling staging bytes must not cancel a canonical commit.
- Keep migration, export, restore, fixity, and shared-reader protection explicit. This proposal authorizes no live migration or cleanup.

Local storage conversion remains opt-in until the compatibility decision and format upgrade are reviewed. Unsupported conversion leaves the existing archive usable and reports sharing as unavailable. Cancellation is an additive, bounded operation, not a background scan. Neither capability runs a model.

## Capabilities

### New Capabilities

- `shared-original-storage`: Shared original bytes within a vault and compatible encryption context, with independent filings, authorized resolution, durable publication, and compatible custody.

### Modified Capabilities

- `client-artifact-preservation`: Explicit cancellation of held uploads; preserve truthful per-file outcomes and current logical duplicate behavior.
- `attachment-source-ingestion`: Separate a filing's logical address from the location of its shared bytes, subject to the compatibility decision.

The existing Cloud collection and original fixity contracts remain binding. Their requirements must change only if the completed design demonstrates a concrete gap.

## Impact

The implementation will use the existing original resolver, capture and preservation leaves, media storage, held-upload ownership checks, and generated tool surfaces. It introduces no external storage service.

The closest existing mechanisms are Cloud reference reuse, archive member publication, the canonical binary metadata index, and resumable upload cancellation. They do not supply portable local sharing or cancellation of held handles. A local pool would add format compatibility and shared-object lifecycle maintenance; those costs belong in the design.

The outcome check must preserve separate access and grant behavior while proving shared bytes, exact export and restore, and cancellation after failure. Existing protected-dataset isolation remains a regression boundary.

The local layout decision blocks the local storage contract and its implementation tasks. It does not block the cancellation contract or the existing restorative release.

## Engineering References

The design uses the smallest relevant guidance from [tool design](https://www.anthropic.com/research/writing-tools-for-agents) and [runtime containment](https://www.anthropic.com/research/how-we-contain-claude). One explicit operation exposes cancellation; storage reuse never grants access.

[Windows hard-link constraints](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createhardlinkw) prevent treating filesystem links as a portable sharing contract.
