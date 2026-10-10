## Context

Investigation of origin/main 754e91006 found that the records needed to measure effect already exist: the first-surfaced ledger, the manual decision records with their family, the stored due-state projection and the Dreamer's deliveries. Two of the stamping calls omit the family, and nothing aggregates the records.

## Decisions

### D1. The upkeep reason is a closed enum from the process that knows

`reason` takes one of four values that this response defines. The serving process reads the worker's live state. Any other process (the CLI, a client without the worker) reads what the worker recorded in its sidecar. The worker records its health at the end of a tick and, new here, within one poll of its gate holding it for a reason other than the one on record. It writes at most once per poll, so a gate that flips between reasons on every request costs one small write per poll. A missing sidecar therefore means no worker has run against this state root. A sidecar that exists but that no tick has written reports `no_tick_yet` with the recorded wait.

### D2. Effect is counted from existing records over a fixed window

The window is seven days, stated in the response. The counts are:

- `surfaced`: identities first stamped on the ledger in the window, plus first Dreamer deliveries for upkeep families.
- `dismissed` and `snoozed`: items with a manual decision of that action and family updated in the window.
- `cleared`: surfaced in the window, no decision on the item, and absent from the family's current set.
- `open`: surfaced in the window, no decision, still in the current set.

The current set comes from the stored due-state projection and the Dreamer's open candidates. Building the block runs no audit. A family that neither holds reports `cleared` and `open` as `unknown`, never 0.

The count is named `cleared`, not `acted`: deleting or withholding a page clears an item too.

### D3. Unattributed rows are not guessed

A ledger row written before this change carries no family. A fused attention item that two families flag carries none either, for the reason `apply_for_item` gives: charging either family would count a surfacing the other made. Both count under `unattributed`. When a row resurfaces through a stamping call, the ledger's existing family update attributes it from the live item.

### D4. The counts are the owner's aggregate

Both the dismissal counts and the effect block reduce every decision record, withheld pages included. Another audience gets the `owner_only_aggregate` refusal instead of a number.

### D5. A due-state reference addresses its own signal

The due-state block publishes a reference whose stored entry names one page and one finding. Resolving it through the review surface ran three whole-vault audits. The resolver now reads the stored entry and re-runs that category's check on its page. It answers only when the re-check reproduces the stored id, a single category and the stored fingerprint; otherwise the whole-vault path runs.

The trade-off: on the review surface, a page's unpartitioned signals (for example `relation_debt`) fold into the partitioned item. A decision through the due reference now records the due signal alone. The review surface keeps that item open for its page-level signals until someone triages it there. A caller that round-trips the review surface's fingerprint as `expected_fingerprint` still resolves the fused item, as before.

The categories that one page settles are `prediction_window`, `question_aging`, the page-local half of `supersession_integrity`, and per-page `unreflected_observations` entries. `unfinished_experiments` carries no partition and shares its id with other page-level queues, so it keeps the whole-vault path.

## Risks

- The wait record costs at most one small write to the Dreamer's sidecar per poll (30 s).
- A due reference on a page with page-level signals now resolves to a narrower item than the review surface lists (D5).
