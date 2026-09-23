## Context

`egress.filter_withheld_entries` is the terminal entry filter every dispatcher result passes. It decided the fields it enumerated (`path`, `target`, `id`, the mutation vocabulary) and dropped an entry naming a withheld page. Derived graph and review structures use other names for the same thing, and some compute counts and caps before any decision. A filter at the end cannot repair a number or a cap, and cannot see an identifier in a field it does not read.

## Goals / Non-Goals

**Goals:**

- A restricted caller's derived answers read as if withheld pages were absent, measured by twin vaults.
- The owner's answers and costs do not change.
- A write by a restricted writer neither resolves onto nor changes a page it may not see.

**Non-Goals:**

- Change any tool schema or description.
- Change the owner's view under any policy.

## Decisions

### Identifier fields are enumerated, with a suffix rule for new ones

The entry filter decides the listed fields and any key ending in `_path`, `_key`, `_anchor`, `_source`, `_target` or `_ref`. A non-path value in such a field is never decided, so the rule costs nothing where it does not apply, and a new derived field fails closed on arrival. A `file:` node key and a vault URI are unwrapped; an extensionless reference is also decided as the page it names. A proposed relation `bullet` is scanned for wikilinks; an entry whose text links a withheld page is dropped rather than rewritten, because the text is the proposal.

### Decide before assembly, for callers other than the owner

`restricted_release_filter` returns `None` for the owner and on an ungoverned vault, and the per-page release predicate otherwise. Derived structures take it and apply it where they choose candidates: a graph walk treats a withheld page as excluded (never a seed, hop, endpoint or edge author), relation generators decide targets and evidence pages before per-method caps, the queue decides source pages in priority order before its cap, timelines treat a pointer to a withheld page as a pointer to nothing, entity lookups decide matches before the status, and listings collapse a folder that holds only withheld files. `visible_page_filter` keeps a reference that names no file (an unresolved link, a placeholder), because nothing can be withheld there and the absent twin keeps it too. The owner's calls keep their original shape.

### Writer links resolve over the writer's view

`vault.writer_link_visibility` is the same predicate for writes. `normalize_wikilink` and the corpus resolution used by the capture sweep accept it and restrict stem, title and path matches to it. A path naming no file (the writer's pending page, a forward reference) is unaffected.

### Write doors decide their target first

`write_target_withheld` is true only for an existing file the writer may not see. Each door checks it at the point where it already answers a missing target, and raises that same answer, so the refusal is the absent refusal by construction.

### Controls

- Dropping an entry, collapsing a folder, and answering a withheld write target as missing. What they prevent: a restricted caller learning that a withheld page exists, what it is called, or what it links, and a restricted writer changing it. Cost when they fire wrongly: a restricted caller misses an entry or cannot write a page it could not read either; that caller pays. The owner never pays: every one of these is `None` or `False` for the owner.
- Entity creation when the only match is withheld proceeds, and the owner reconciles the duplicate later. That trade-off is accepted: the alternative answers differently for a withheld entity and an absent one.

## Risks / Trade-offs

- A folder delete by a restricted writer that holds both visible and withheld files, and a create whose path collides with a withheld file, cannot answer exactly as the absent case without either changing the withheld page or changing the contract. They are left for a ruling.
- Link resolution itself still runs over the whole vault for restricted readers; that is the next part of this change.
