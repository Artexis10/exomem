# Exomem programme index

OpenSpec owns the programme requirements and task plans. This index connects
those contracts and records the delivery dependencies recovered on 2026-10-07.
A checked task needs implementation, verification and merge evidence.

## Current delivery sequence

| Work | Contract | Dependency and outcome |
| --- | --- | --- |
| RAW originals | [Collection query engine](changes/add-collection-query-engine/tasks.md), S1.5b | Admit protected originals before ranking or reporting. Ship before the first SQLite owner slice. |
| Compiler follow-up | [Memory loop](changes/close-memory-loop/tasks.md) and [thread-aware compilation](changes/archive/2026-10-06-add-thread-aware-compilation/tasks.md) | Integrate RAW admission. Preserve truthful withheld results and useful context without repeated classification. |
| Release | [Plain Cloud cells](changes/adopt-exomem-cloud-plain-cells/tasks.md) | Release 0.110 follows merged RAW and compiler corrections, with the Cloud storage and runtime changes. |
| Source-kind retirement | `retire-other-source-kind` on its delivery branch | Follow RAW and compiler. Require meaningful agent classification and teach the migration through every bootstrap profile. |
| Vocabulary registries | `add-vocabulary-registries` on its delivery branch | Follow source-kind retirement. Replace customer vocabulary constants with vault-owned registries. |
| First SQLite collection | [Collection query engine](changes/add-collection-query-engine/tasks.md), S1, and [SQLite collections](changes/move-structured-collections-to-sqlite/tasks.md) | Follow RAW. Prove startup compatibility, location admission, import, publication and the owner workflow. |
| Graph follow-on | [Graph intelligence](changes/add-graph-intelligence/tasks.md) and [graph traversal](changes/add-graph-traversal-queries/tasks.md) | Follow the first SQLite collection. Preserve authored relation semantics and prove request-time utility. |

The source-kind and vocabulary changes remain local delivery branches until
integration. Their task files become links here when those branches merge.
The first SQLite delivery does not complete every query or storage phase.

## Dynamic vocabulary programme

The registry change carries source kinds, entity and relation types, semantic
categories and unit roles. Its follow-ons cover skill guidance, lifecycle
statuses, note types, attributes, vault conventions, language packs, model
judgement, advisory nudges and Planning values.

Skill guidance, lifecycle statuses, attributes and Planning can proceed
independently after the registry foundation. Language packs depend on the
compiler continuity decision. Model replacements require measured ablations.
See [vocabulary evolution](changes/activate-agent-led-vocabulary-evolution/tasks.md),
[vocabulary reconciliation](changes/reconcile-vocabulary-variants/tasks.md) and
[vault-owned conventions](changes/make-activation-conventions-vault-owned/tasks.md).

## Governed vault consolidation

[Vault consolidation](changes/add-governed-vault-consolidation/tasks.md) now has
three deliveries. Connector admission preserves owner identity while enforcing
host-configured content ceilings and protection across supported portability.
Private vocabulary domains follow the vocabulary registry foundation. Offline
managed-vault import follows both deliveries and uses existing mutation and
restore owners.

Connector admission alone does not complete T16. Real import and connector
cutover need fresh operational authority after disposable rehearsal proves
preservation, allowed utility, negative disclosure, and rollback. Source
retirement remains a separate destructive operation with verified surviving copies.

The retired stack supplies salvage candidates only. Its signing, custody,
fencing, and per-step approval protocol is not the implementation contract.
Hosted end-client provenance remains a gateway dependency; a shared cell bearer
receives the restricted connector default when protection is armed.

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
interface. These are separate from the immediate RAW and compiler release.
