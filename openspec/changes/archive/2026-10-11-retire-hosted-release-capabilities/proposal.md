## Why

Exomem retired the Hosted platform for Exomem Cloud (`adopt-exomem-cloud-plain-cells`, task 7.2). Three canonical capabilities describe only deleted Hosted code, so they must leave the canonical specs in the same delivery as that code.

## What Changes

- Retire `hosted-image-candidate-publication`: the Hosted runtime and provisioner image candidates are no longer built.
- Retire `hosted-database-budget`: the Hosted control-plane database budget audit is deleted.
- Retire `hosted-plugin-identity`: the Hosted plugin candidates are deleted.

At this initial retirement boundary, the other `hosted-*` capabilities stayed. Active changes carried deltas on `hosted-gateway-contract`, `hosted-agent-surface` and `hosted-marketplace-release`. `hosted-secret-custody` describes the BWS source of the secret handoff, which Exomem Cloud still uses. `hosted-mutation-safety` covers kept local and Cloud behavior. `hosted-vault-portability` and `hosted-tenant-cell` describe deleted Hosted code and also carry active deltas. These five capabilities blocked removal at that boundary. The later reconciliation archives the wholly superseded reviewer-bootstrap change and retires `hosted-marketplace-release`; four capabilities remain blocked.

## Impact

- Removes three canonical specs. No code changes in this change; the code was deleted in the same delivery.
