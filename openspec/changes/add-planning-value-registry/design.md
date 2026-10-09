# Design

## Where the values live

The values live in the vocabulary substrate, not in `_collection_types/planning.yaml`.
Only the substrate has an overlay, history, restore and owner governance, which a vault-added value needs.
`register_builtins` refuses any change to a built-in declaration's bytes until P4 adds a revision path, so `planning.yaml` stays byte-identical.

One `RegistrySpec` (`planning_values.SPEC`) covers the six governed fields over one overlay.
A key is `<field>.<value>`, so the same value can exist on two fields, and the field is fixed by the key.
The governed fields are the prefixes of the shipped pack; code holds no list of them.
Pack order is presentation order: the bootstrap lists and the default horizon views keep their order.

## Attributes

- `class` on a status: `open`, `done` or `dropped`. Docs call it the planning class; it is not the page lifecycle class of the status registry.
- `parents` on a kind: the kinds that may be its parent. An empty list means the kind takes no parent.

Both are fixed once saved, because a stored item's archive and hierarchy rules must not change under it.
`rank` and `order` are not shipped: no code sorts or compares Planning values, so each waits for a consumer.
Entries take no aliases, because an item stores the exact value.

## Readable stored values

`normalize_item` takes the vault root and the stored item. A value equal to its stored value is accepted when it is deprecated or unregistered.
A new or changed value must be an active registered value. A registry restore therefore never makes a collection unreadable.
The archive rule also checks the class only when status or lifecycle changes, or when the status is shipped.
An item whose kind has no readable definition keeps every hierarchy rule except the kind rules.

## Admission

`PlanningValues(root)` reads shipped values without the overlay. A value that is not shipped admits the overlay through the shared `admission_refusal`.
A caller that cannot admit it sees only the shipped values. A write that depends on a vault value then refuses with `PLANNING_VALUES_UNAVAILABLE`.
The audit and due-state open-item rule treats a status with no readable class as not settled, as it treated any unknown status before.
Manifest parses record the registry dependency in the parse cache, so a save or restore takes effect on the next parse.

## Old C4 debt kept

These rules name shipped keys. Shipped values are fixed, so each still resolves, but none reduces to `class` or `parents`:

- the six `kind == "area"` branches (an area carries no delivery state, takes no parent and cannot cross the area boundary);
- the status ladder for `candidate`, `planned`, `active`, `blocked` and `completed` against `commitment` and `horizon` values; a vault status takes only the class rule;
- `commitment == "committed"` in the required-parent rule;
- the capture defaults in `normalize_item` and their prose copy in the bootstrap `default_capture`;
- the in-flight slice of `plan_progress` (`status == "active"`, `commitment == "committed"`);
- the six horizon views in `_collection_types/planning.yaml`, until P4.
