## Why

`classify-sensitive-material-at-capture` lets an agent label a page `identity`, `health` or `financial`. The label stays on that page only. A note that summarises a classified Source, or an episode about a classified record, carries no label, so a later policy that restricts the Source would still release the summary.

A restriction also leaks when the reader can tell that something is withheld. A page that is missing for a restricted caller must look like a page that does not exist, on every read door.

So restriction by class is safe only when:

- a page carries the classes of the material it was written from, also when that material is classified later;
- a page whose classes cannot be proven current answers as missing, never as released;
- every release to a non-owner goes through the one egress decision, and that decision reads the inherited classes.

This change delivers those three properties and the policy selector that uses them. Per-audience policy, its registries and live enablement stay in `restrict-disclosure-per-audience`. That change starts only after an adversarial re-attack passes on this one.

## What Changes

- **Effective classes.** A page always keeps its own `sensitivity` classes. It adds a write-time floor and the classes inherited from authorized derivation. An owner mask can remove those added contributions. The store keeps the result in one column that every consumer reads.
- **Carry-over at write time.** An authored write takes the effective classes of:
  - the pages it names in `sources`;
  - the pages it names in `links` whose relation carries derivation;
  - an episode's `about` refs;
  - the page it edits.

  The classes go into a protocol-owned `inherited_sensitivity` field. `add_record` and `edit_record` gain `sources`; the note tools already take `sources` or `links`. Mechanical rewrites, such as link repair after a rename, keep the existing floor.
- **Live inheritance.** A new derived store object, `page_sensitivity`, holds each page's effective and inherited classes and a generation. A class added to a Source later reaches pages with authorized derivation bindings to it.
- **Removals and masks.** Clearing a class on a Source removes it from pages that hold it only by inheritance. A page written after the class was set keeps it in its floor. An owner can mask a floor or inherited contribution with a reason. Dependents still inherit any own class with the same key.
- **Read results ask for sources.** A read result that releases a classified page tells the agent to name the page in `sources` if a write uses it.
- **Withheld equals missing.** Under a policy that selects by class, a non-owner caller gets as missing any page whose inherited classes are not current at the read's snapshot.
- **One egress decision.** The existing result postfilter decides on effective classes. A test runs every registered read under a restricted caller over synthetic classified pages and fails on any leak.
- **A `sensitivity` scope selector.** A scope may select pages by effective class. Such a scope only ever lowers a ceiling: it never takes a page out of the residual scope. A vault without the vault store refuses a policy that uses it.
- **Case.** Path selectors match regardless of letter case.

## Capabilities

### New Capabilities

- `sensitivity-lineage`: effective classes, the write-time floor, live inheritance and its currency proof, removals and masks, and the read-result prompt.

### Modified Capabilities

- `governance-kernel`: the `sensitivity` scope selector and its store-mode requirement, the residual scope's treatment of it, case-insensitive path selectors, and the single egress decision on effective classes.

## Impact

- Code:
  - `page_fields.py` (the protocol-owned `inherited_sensitivity` key; the `sensitivity_mask` pointer field in the `page-fields` pack)
  - the relation registry (a closed behaviour `inherits_sensitivity`, assigned in the core pack)
  - a pre-plan carry-over step in the one write pipeline, and `sources` on the record tools
  - a `page_sensitivity` owner object in the vault store, its derivation, and its currency watermark
  - `governance/policy.py`, `governance/membership.py`, `governance/egress.py`, the projection catalog and collection governance
- Data: no page bytes change at deploy. The store derives `page_sensitivity` on its next derivation. A page gains a floor on its next authored write.
- Disclosure: with no policy, nothing changes for any reader. With a policy that selects by class, no page becomes more visible to any audience than it was.
- Performance: inheritance runs as derivation work in the existing store-thread slices. The restricted first-read target (about one second at 20,000 pages) stays; the change measures it before and after.
