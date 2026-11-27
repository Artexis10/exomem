## Why

`classify-sensitive-material-at-capture` lets an agent label a page `identity`, `health` or `financial`. The label stays on that page only. A note that summarises a classified Source, or an episode about a classified record, carries no label, so a later policy that restricts the Source would still release the summary.

A restriction also leaks when the reader can tell that something is withheld. A page that is missing for a restricted caller must look exactly like a page that does not exist, on every read door.

So restriction by class is safe only when:

- a page carries the classes of the material it was written from, also when that material is classified later;
- a page whose classes cannot be proven current answers as missing, never as released;
- every door that releases content to a non-owner decides through the same egress check.

This change delivers those three properties and the policy selector that uses them. Per-audience policy, its registries and live enablement stay in `restrict-disclosure-per-audience`. That change starts only after an adversarial re-attack passes on this one.

## What Changes

- **Effective classes.** A page's effective classes are the union of:
  - its own `sensitivity` field;
  - a new protocol-owned `inherited_sensitivity` field, set at write time;
  - its live lineage: the effective classes of the pages it was derived from, recomputed when a class or a derivation edge changes.
- **Carry-over at write time.** Every write surface runs one pre-plan step that sets `inherited_sensitivity` from:
  - the pages the write cites;
  - the page it edits;
  - the pages the writing agent names in a new optional `drew_on` argument.

  The plan function refuses a write whose field holds fewer classes than that step computes. Only the classify operation lowers the field.
- **Live lineage.** A new derived store object, `page_lineage`, holds each page's lineage classes and a generation number. The store thread recomputes it in the same transaction that changes a page's classes or a derivation edge. A class added to a Source later reaches every page derived from it.
- **Removals.** Clearing a class on a Source removes it from pages that hold it only through lineage. A page written after the class was set keeps it, because its write-time field holds it.
- **Session read record.** The governance sidecar records which classified pages each session was shown. Each write result lists the classified pages the session was shown that the write did not cite, edit or name in `drew_on`, so the agent can correct the write. The record is evidence only: no class reaches a write because of it.
- **Withheld equals missing.** For a non-owner caller under a policy that selects by class, a page whose lineage is not current at the read's snapshot answers as missing, in shape and in timing.
- **One egress decision.** The design lists every door that releases page content to a non-owner. A test fails when a door reads the store snapshot without the egress decision. Owner-only doors, such as Markdown export and backup, are exempt.
- **A `sensitivity` scope selector.** A governance scope may select pages by effective class, and `exclude` may carry it. A vault without the vault store refuses a policy that uses it.
- **Two leaks found earlier, closed here.** Path selectors match regardless of letter case. A create that hits a page the caller cannot read refuses without saying that the page exists.

## Capabilities

### New Capabilities

- `sensitivity-lineage`: effective classes, the write-time field and `drew_on`, live lineage and its currency proof, removals, the session read record and its report in write results.

### Modified Capabilities

- `governance-kernel`: the `sensitivity` scope selector, the store-mode requirement for it, case-insensitive path selectors, the non-revealing create refusal, and the single egress decision for non-owner release doors.

## Impact

- Code:
  - `page_fields.py` and the `page-fields` pack (the `inherited_sensitivity` field)
  - the pre-plan names step of the one write pipeline (`one-write-pipeline` D4) and every write surface's `drew_on` argument
  - a `page_lineage` owner object in the vault store, with its rederive version and currency proof
  - `governance/policy.py`, `governance/membership.py` (selector, subject fields, memo keys)
  - `governance/egress.py` and each listed release door
  - the governance sidecar (`.governance.sqlite`) for the session read record
  - the relation registry: a closed behaviour `inherits_sensitivity`, assigned in the core pack
- Data: no page bytes change at deploy. The first write to a page after release sets its `inherited_sensitivity`. The store derives `page_lineage` for every page on its next derivation.
- Disclosure: with no policy, nothing changes for any reader. With a policy that selects by class, more pages are restricted than before, never fewer.
- Performance: the lineage recompute runs on the store thread under the existing 100 ms hold bar, in bounded chunks. The load target and the restricted-read budgets in `make-vault-store-canonical` D13 stay as they are.
