## Why

One owner can use several connectors with different content permissions. Owner identity currently risks bypassing that separation, which prevents safe consolidation of managed vaults.

This change keeps one governed destination useful while preventing a connector's hidden corpus from influencing its observations. The durable decision is a host-configured connector ceiling, followed by private vocabulary domains and an offline import path.

## What Changes

- Delivery A adds non-bypassable content ceilings for authenticated connectors without removing owner identity or legitimate allowed-target write authority.
- Host configuration maps verified issuer/client bindings to denied canonical Scope IDs and defines a restrictive default for unknown clients.
- Admission precedes decoding, candidate selection, ranking, graph assembly, aggregation, response construction, and target mutation.
- Configured capture namespaces preserve public visibility across all writers and restores. Creation eligibility precedes existence and collision observations; admitted existing edits continue elsewhere.
- Durable arming survives supported downgrade, backup, export, and restore. Armed archives use manifest version 2 with canonical protective selectors; unarmed archives retain version 1.
- Delivery B adds private vocabulary domains after the vocabulary registry foundation. Public definitions remain explicit; private definitions never become a public union.
- Delivery C adds offline managed-vault inventory, reconciliation, import, recovery, and rollback using existing mutation and restore owners.
- Real import, connector cutover, and source retirement remain separately authorized operations. Capability shipment does not perform them.
- **BREAKING when armed:** missing or incompatible host configuration prevents serving; unknown connectors receive the restricted default, including shared-cell bearers.

## Capabilities

### New Capabilities

- `vault-consolidation`: Connector-separated managed vaults, private vocabulary domains, and lossless offline reconciliation and import.

### Modified Capabilities

- `command-surface`: Preserve verified connector and originating authentication context across existing surfaces and delegated transfers.
- `release-gate`: Apply connector ceilings before owner shortcuts and before hidden contributors can affect observations.
- `hosted-mutation-safety`: Preserve allowed owner writes, prevent self-widening, and arm only under stopped maintenance authority.
- `governance-kernel`: Distinguish exact protective restore continuity from content admission membership while preserving canonical proposal recovery.
- `hosted-vault-portability`: Preserve armed requirements through versioned export and fresh-state restore without copying source connector authority.
- `disclosure-evidence`: Keep receipts plaintext-free, access-limited, append-only, and outside policy and knowledge.
- `product-e2e`: Prove connector separation and allowed utility through real authentication, delegation, downgrade, and portability workflows.

## Impact

Delivery A changes principal propagation, shared admission, content producers, write admission, compatibility enrollment, and portability. It adds no reasoning model, signer service, public consolidation command, or Hosted profile.

Delivery B follows the vocabulary registry foundation. Delivery C follows both earlier deliveries and retains the complete preservation contract in the vault-consolidation delta.

The retired eleven-action signed-source saga is no longer the design. Existing reconciliation, fingerprint, preimage, and recovery code may be salvaged against this contract. The old stack is not an integration or verification target.

Delivery A is independently useful. It does not complete T16 or authorize combining real vaults. Hosted end-client provenance requires gateway work outside this repository; shared-cell identity alone cannot provide it.
