## Why

Maps activation gives an agent topic context, but it does not carry the owner's global stance or a record of the response's context. Recovery instructions also differ between clients. This change adds bounded stance data, central next moves and a private response record.

## What Changes

The first increment keeps the existing map and turn slice. It adds these mechanisms:

- **Stance block.** Eligible live compiled pages can carry `stance: true`. Their admitted units form a bounded block in maps responses. Existing unit admission and terminal egress protect its text, references, counts and generation. `EXOMEM_ACTIVATION_STANCE=off` removes the block.
- **Typed currency.** Rich units gain strict `as_of` and `until` dates and reuse `check_by`. Supersession and archive remain authoritative. Expired units become history without a write; a passed review date adds `review_due` without invalidating the unit.
- **Served-context record.** A bounded append-only table in tenant state records the final authorized response metadata, including surfaced upkeep identities. Each successful recording returns a fresh `served_context_id`. Owner lookup and summaries describe retained rows. Opened and cited observations remain unknown; the record proves neither client receipt nor use.
- **Next move.** One source supplies the per-state `next_action` text for activation and search. The hook renders the move and uses a generated fallback for an older service. Existing anchors recovery and reduced-surface bootstrap carriers remain available.
- **Stance capture.** The client model proposes a global line with its source when the engagement policy permits it. The owner must authorize the line and any promotion of existing page content. An explicit request for that exact action already supplies authorization.
- **Source coverage.** An empty turn slice explains that activation covers compiled material and that search can reach captured Sources.

The existing map field `activity` keeps its name and meaning. Task-specific activities, guidance declarations and entity scope remain deferred.

## Evidence and scope

The development measurements inform this increment; they establish no answer-quality gain. The incident comparison has two cases, and its case-level intervals include zero. The large inline-content comparison is inconclusive because the actor received oversized-result notices. The first development run of the built change must inspect actor-visible delivery and measure its behavior.

The task layer and the check sentence did not meet their development gates. Server-side correction detection stays out. The retained next moves still need real-model proof; an observed activity-declaration effect does not prove the different moves in this increment.

## Capabilities

### New Capabilities

- `stance-block`: eligible stance pages, bounded caller-visible lines, currency, caching and rollback.
- `served-context-record`: response metadata, retention, truthful owner lookup and retained-row summaries.
- `read-next-action`: central moves, capability-aware calls and shared compatibility text.

### Modified Capabilities

- `context-activation`: typed unit currency and the empty-slice coverage sentence.
- `semantic-unit-language`: strict governed `as_of` and `until` metadata.
- `context-miss-learning`: an incident can link recorded response metadata and name its limits.
- `agent-bootstrap-contract`: next-move guidance and the stance capture route, preserving engagement and carrier selection.
- `retrieve-inject-hook`: bounded stance rendering and central moves with older-service recovery.

Some affected requirements live in active changes. Reconcile their wording before implementation and preserve later scenarios when synchronizing them.

## Impact

The implementation extends the existing parser, currency, catalogue, unit admission, response budget and capability rendering. It adds a stance projection, one table with retention and one owner read-out leaf. It adds no model, classifier, open/cite tracker or ranking change.

The stance cap starts at 800 characters, subject to remaining counted-prose space. The move reserves 240 characters. These are initial engineering bounds, not measured optima. Serialized structure remains outside `max_chars`, and `serialized_chars` must describe the final response.

Rows and refs stay in tenant state and out of host logs. Recording has bounded best-effort latency and cannot fail the read. Existing surfaced-item ledgers remain until a replacement proves equivalent identity, surface coverage and durability.

Publication follows the normal review and delivery rules. Install-dependent checks retain their separate deployment dependency; there is no publication freeze.
