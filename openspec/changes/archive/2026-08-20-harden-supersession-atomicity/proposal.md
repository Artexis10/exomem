## Why

Supersession promises to preserve history — the old page stays readable and points to the new one. But `replace` reads the old page's status, then writes the new page, the old page, and the log SEQUENTIALLY with no lock or compare-and-swap. Two concurrent `replace` calls on the same active page can both pass the "already superseded?" check and race the pointer writes, and the last old-page write can drop the other successor pointer. A crash between the new-page write and the chain updates leaves a dangling standalone new page. (Audit finding CDX-04, HIGH-plausible; the common sequential path is correct — this is the concurrency/partial-commit edge.)

Evidence: `src/exomem/replace.py` old-status check (~119-127) has no CAS around the read; `_mark_superseded` and the new/old/log writes are sequential (~131, ~181-187); `vault.batch_atomic_write` documents partial commits (~173-175).

## What Changes

- Guard the old-page status read + flip with a compare-and-swap (e.g. the old page's `expected_hash` held across the whole supersession transaction) so a concurrent replace that changed the old page is refused.
- Stage all pages of a supersession (new page, old page's pointer/status flip, log) in ONE atomic write batch so a partial commit cannot leave a dangling new page or a half-updated chain.

## Capabilities

### New Capabilities

- `supersession-atomicity`: Guarantees that a supersession either fully commits its bidirectional chain or makes no change, and that concurrent supersessions of the same page cannot both win.

## Impact

Affects `src/exomem/replace.py` and its use of `src/exomem/vault.py` atomic write. No change to the read path or MCP tool schema. Existing `tests/test_replace.py` / `tests/test_supersession_surface.py` must stay green; new tests cover the concurrent-replace and mid-transaction-failure cases.
