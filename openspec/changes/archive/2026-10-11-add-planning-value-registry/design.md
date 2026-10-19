# Design

## Where the values live

The values live in the vocabulary substrate, not in `_collection_types/planning.yaml`.
Only the substrate has an overlay, history, restore and owner governance, which a vault-added value needs.
`register_builtins` refuses any change to a built-in declaration's bytes until P4 adds a revision path, so `planning.yaml` stays byte-identical.

One `RegistrySpec` (`planning_values.SPEC`) covers the six governed fields over one overlay.
A key is `<field>.<value>`, so the same value can exist on two fields, and the field is fixed by the key.
The governed fields are the prefixes of the shipped pack. `normalize_item` validates each one that an item carries; a deliverable must carry each governed field an area may not carry.
Pack order is presentation order: the bootstrap lists and the default horizon views keep their order.

## Attributes

- `class` on a status: `open`, `done` or `dropped`. Docs call it the planning class; it is not the page lifecycle class of the status registry.
- `parents` on a kind: the kinds that may be its parent. An empty list means the kind takes no parent.

Both are fixed once saved. A restore can still remove an entry, and a later save can register the same key with another meaning, so stored items never depend on either attribute (see below).
`rank` and `order` are not shipped: no code sorts or compares Planning values, so each waits for a consumer.
Entries take no aliases, because an item stores the exact value.

## Readable stored values

`normalize_item` takes the vault root and the stored item. A value equal to its stored value is accepted when it is deprecated or unregistered.
A new or changed value must be an active registered value. A registry restore therefore never makes a collection unreadable.
The archive rule also checks the class only when status or lifecycle changes, or when the status is shipped.

Registry-derived rules judge only the writes that depend on them:

- The item a write changes reads its kind's registry `parents` only when the write can tighten them: a new item, a new kind or parent, a commitment that becomes committed, or a lifecycle that becomes `active`. A withheld definition refuses with `PLANNING_VALUES_UNAVAILABLE`; an unregistered kind refuses as invalid. Archiving, decommitting and other edits keep the shipped rules, so they work after a kind is removed.
- A write that changes an item's kind also checks each direct child against that child's registry `parents`, because `parents` constrains only the direct parent. A withheld child definition refuses; a removed one has no rule; only a violation this write introduces refuses.

Every other item keeps the shipped kind rules, which no save or restore can move; an item of a vault kind keeps only the structural rules (active targets, area agreement, cycles, archived parents).
Reads therefore never depend on the registry, and a restore followed by a save with other `parents` leaves the collection readable.
`validate_hierarchy` takes `write` as a required keyword: read, revise and import paths pass `write=None`, so a new write path cannot fall back to read rules unnoticed.

## Admission

`PlanningValues(root)` reads shipped values without the overlay. A value that is not shipped admits the overlay through the shared `admission_refusal`.
A caller that cannot admit it sees only the shipped values. A write that depends on a vault value then refuses with `PLANNING_VALUES_UNAVAILABLE`.
The audit open-item rule treats a status with no readable class as not settled, as it treated any unknown status before.
The due-state write delta classifies with `PlanningValues(root, server_side=True)`, which reads the overlay without the writer's admission. That matches its unfiltered snapshot: the stored projection must not depend on who wrote last, and disclosure is decided at serve.
The default horizon views quote a value that YAML would read as another type, such as `off` or `null`.
Manifest parses record the registry dependency in the parse cache, so a save or restore takes effect on the next parse.

## Old C4 debt kept

These rules name shipped keys. Shipped values are fixed, so each still resolves, but none reduces to `class` or `parents`:

- the six `kind == "area"` branches (an area carries no delivery state, takes no parent and cannot cross the area boundary);
- the status ladder for `candidate`, `planned`, `active`, `blocked` and `completed` against `commitment` and `horizon` values; a vault status takes only the class rule;
- `commitment == "committed"` in the required-parent rule and in `_tightens`, both through `_committed`;
- the capture defaults in `normalize_item` and their prose copy in the bootstrap `default_capture`;
- the in-flight slice of `plan_progress` (`status == "active"`, `commitment == "committed"`);
- the six horizon views in `_collection_types/planning.yaml`, until P4.

## Named debt from review

- N1: a vault kind may name `area` in `parents`. No declared attribute marks a kind as a non-deliverable, and a key comparison would be new C4 debt; refusing it waits for such an attribute.
- N2: the served due-state can show a restricted reader one bit per private status (open or settled). The serve boundary must decide that.
- N3: the store's view edit-back (`collection_store/writer.py` `_classify_view_input`) validates without the stored row, so a store item that keeps a removed vault value reads as `VIEW_INVALID`. The store is dark for Planning.
- N5: the bootstrap `planning` lists grow with each vault value, up to the 512-entry cap. A cap or a pointer to `schema_memory` waits for a measured need.
- N8: on a bound store writer, an owner's overlay admission walks the tree (`egress.owner_only_aggregate`) for writes and manifest-cache hits that meet a vault value. Measure it before the store goes live.
- N9: bootstrap lists no Planning statuses or health keys, as on main; an agent reads them through `schema_memory(subject="planning-values", operation="inspect")`. The `vocabulary` section omits the registry's keys (`summarize_keys=False`) because the `planning` block lists kinds, horizons, priorities and commitments; it still reports the registry's findings, `new` keys and `unavailable`.
