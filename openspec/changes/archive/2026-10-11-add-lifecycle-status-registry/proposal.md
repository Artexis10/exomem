# Proposal

## Why

Page status labels currently select behavior through several Python lists and comparisons.
A vault cannot define another label without changing code, and warm results can retain outdated classification.

## What Changes

- Add the status registry through the existing vocabulary loader, pack, overlay, guarded save, history and restore contract.
- Map authored labels and aliases to five closed classes: `live`, `pending`, `superseded`, `retired`, and `abandoned`.
- Keep shipped canonical meanings stable independently of private extensions; require admission only for extension-dependent classification.
- Keep absent or empty page status live without registry admission or unregistered debt.
- Replace status-key comparisons with purpose-specific class predicates across activation, find, semantic validation, currency and recurrence.
- Keep unknown labels live with visible debt after successful registry admission; distinguish unavailable classification from an unknown label.
- Bind reusable results to the effective registry digest so the next operation observes a committed save or restore.
- Preserve pending named carry, authored supersession, page bytes, ranking magnitudes and existing resource limits.
- Correct the recovered plan wording: planned evidence pages stop supplying recurrence evidence; canonical planned entity resolution remains unchanged.
- Teach the fixed class rule through a new semantic contract version, with admitted registry context separate from its normative digest.
- Keep historical descriptors and authoring artifacts immutable; version corrective bootstrap guidance for their current shared runtime behavior.

## Capabilities

### New Capabilities

- `lifecycle-status-registry`: governed page status definitions, purpose-specific classification, honest debt, disclosure and immediate save/restore visibility.

### Modified Capabilities

- `semantic-write-contract`: minimum-unit applicability uses the effective `live` class instead of a literal inactive-status list.

## Impact

This is vocabulary task S3 and depends on the actual merged registry foundation.
Implementation adds one adapter and pack, explicit snapshot parameters, and dependencies in existing caches.
It adds no catalogue columns, second cache, model, scheduler or tool parameter.
Typed note creation enums remain S4b; Planning, Records, job and receipt states retain their separate contracts.
Private vocabulary domains remain the connector-boundary programme's responsibility.
