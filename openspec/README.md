# Exomem programme index

OpenSpec owns the programme requirements and task plans. This index connects
those contracts and records the delivery dependencies as of 2026-10-08.
A checked task needs implementation, verification and merge evidence.

## Current delivery sequence

| Work | Contract | Dependency and outcome |
| --- | --- | --- |
| Lifecycle statuses | `add-lifecycle-status-registry`, delivered by PR #1630 | Follows the merged registry foundation. Classify page lifecycle through registry classes; keep restricted connectors' allowed writes useful. |
| Source reclassification | [Reversible reclassification](changes/make-source-reclassification-reversible/tasks.md) | Make moves byte-safe and reversible, then preview and drain the legacy `Sources/Other` files. History-aware reads start only if the preview holds files back. |
| First SQLite collection | [Collection query engine](changes/add-collection-query-engine/tasks.md), S1, and [SQLite collections](changes/move-structured-collections-to-sqlite/tasks.md) | Prove startup compatibility, location admission, import, publication and the owner workflow. Admitted point writes and Planning values follow its merge. |
| Connector ceilings and private vocabulary | [Vault consolidation](changes/add-governed-vault-consolidation/tasks.md), Deliveries A and B | Follows lifecycle statuses. Enforce host-configured connector ceilings and per-instance vocabulary without new caches or journals. |
| Compiler literal support | [Memory loop](changes/close-memory-loop/tasks.md), T2e | Follows lifecycle statuses. Measure quality after the source freeze, against the canonical latency gates. |
| Graph follow-on | [Graph intelligence](changes/add-graph-intelligence/tasks.md) and [graph traversal](changes/add-graph-traversal-queries/tasks.md) | Follows the first SQLite collection. Preserve authored relation semantics and prove request-time utility. |

Shipped and archived: RAW originals, the compiler follow-up and
[source-kind retirement](changes/archive/2026-10-07-retire-other-source-kind/tasks.md).
The first SQLite delivery does not complete every query or storage phase.

## Dynamic vocabulary programme

[The registry foundation](changes/add-vocabulary-registries/tasks.md) is merged.
It carries source kinds, entity and relation types, semantic categories and unit
roles, and it sequences the follow-ons: skill guidance, lifecycle statuses, note
types, attributes, vault conventions, language packs, model judgement, advisory
nudges and Planning values.

Note types and vault conventions follow lifecycle statuses. Attributes follow
source reclassification. Planning values follow the first SQLite collection.
Language packs depend on the compiler literal-support decision. Model
replacements require measured ablations. See
[vocabulary evolution](changes/activate-agent-led-vocabulary-evolution/tasks.md),
[vocabulary reconciliation](changes/reconcile-vocabulary-variants/tasks.md) and
[vault-owned conventions](changes/make-activation-conventions-vault-owned/tasks.md).

## Governed vault consolidation

[Vault consolidation](changes/add-governed-vault-consolidation/tasks.md) holds
three deliveries: A, connector ceilings and allowed writes; B, private vocabulary
domains; C, offline managed-vault import. The retired trust-chain saga is not
part of it.

The retained outcome is one governed vault with distinct connector boundaries.
An owner identity must not bypass a connector's content restriction. Establish
that boundary before combining vaults. Prove preview, import, admitted reads and
rollback on disposable copies before changing live vaults. Preserve original
evidence and use the existing policy and mutation owners. Shipping the
capability does not authorize a live import or connector cutover.

## Remaining programme contracts

[The memory loop](changes/close-memory-loop/tasks.md) owns integrated acceptance,
client coverage, history, methods, maintenance and closure. It links its storage,
graph, vocabulary and benchmark dependencies.

[The sensed model](changes/add-sensed-epistemic-model/tasks.md) owns further
interpretation and correction work. [The activation benchmark](changes/add-context-activation-benchmark/tasks.md)
owns activation evidence; the [competitive benchmark](changes/add-competitive-benchmark-programme/tasks.md)
owns external calibration. Paid comparisons remain deferred.

Hosted RAW needs a verified tenant-owner binding before its current exemption
can end. Desktop acceptance needs the selected source export and product
interface.
