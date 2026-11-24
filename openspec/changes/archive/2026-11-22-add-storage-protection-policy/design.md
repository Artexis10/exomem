# Design

## Use the existing custody authority

The desired policy belongs in the owner-managed media store configuration. A missing policy leaves the existing scheduler and eviction contract active. Configuration names eligible stores and a vault-local failure domain when a local original counts. Store descriptors supply remote failure domains. Unknown descriptors supply no proof of independence.

The media ledger already records exact replicas, decrypt samples, key exports, and quarantine. Reuse those facts and the existing copy queue, transfer workers and store adapters. Use the physical object key for counting and a canonical binding witness for each filing. A physical replica shared by two filings contributes one copy. The eviction fence checks each filing's post-unlink recovery separately.

## Limit autonomous changes

Policy reconciliation adds and verifies copies. Owner authority covers reductions through the existing guarded media operation boundary. A background eviction cannot infer approval from the presence of the new policy. Check the current policy, binding, proof and quarantine in the existing final ledger transaction. Keep network and full-hash work outside that transaction.

The containment rule agrees with the primary-source guidance on bounding what an autonomous process can do. [Access containment](https://www.anthropic.com/research/how-we-contain-claude)

## Persist bounded work and allowance

Page through a resumable physical-key range. Maintain idempotent work for changed bindings and evidence. Continue bootstrap through ordinary writes and report incomplete inventory until the current frontier is known. Keep policy demand separate from existing repair and owner demand. Reuse exact copy and retry handling when a lost acknowledgement might already have published a version.

Measure the automatic copy budget as transferred ciphertext bytes over a UTC calendar month. Reserve the bounded upload before sending. Settle a confirmed version exactly once. Retain uncertain reservations until the existing version outcome settles or the allocation can be reconciled. Presence checks remain distinct from content proof. Existing configured read and sample limits continue to bound verification.

## Proof and load

Size for 500,000 originals and 25 TB. The owner read uses persisted counts and indexed lookups. A scheduling page performs batched exact proof reads and limits candidates to one work slice. Measure query steps before and after on differently sized generated vaults with equal page sizes. Target a sub-250 ms warm local owner read and a one-second planner slice between attempted copies. A provider copy uses the existing asynchronous transfer limit.

Use the established replica and restore path with a disposable vault, two stores and shared filings. Exercise exact verification, blocked spending and lost acknowledgement. Enumerate managed placement sequences and assert that a still-live original with a verified recoverable copy never loses the last one through a managed mutation.
