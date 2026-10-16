## Context

The dispatcher `writer_lease.invoke_command` is the one entry that MCP, REST, hosted cells and the CLI share, and each surface binds its principal before it calls the dispatcher. `projection_timing` already pads `find` and `ask_memory` into registered completion classes, and each class has a deadline that refuses a request that runs past it. `egress.caller_restricted` answers whether anything, RAW included, could be withheld from the bound caller.

## Decisions

### D1. A pad-only floor, not a registered completion class

The floor is one constant beside the existing classes, with a helper that waits and never raises. A registered class was rejected: its deadline raises a refusal when the request runs long, so a slow withheld read would refuse with a different answer than a fast missing one, and the overrun itself would become the signal. A slow read therefore completes late with its own refusal. The `command.refusal_floor` span records the overrun for the owner, so overruns are visible without being visible to the caller.

### D2. Ask about restriction only after a refusal

The dispatcher calls `caller_restricted` only when a marked command raised. A success never pays for the question. If the question itself fails, the dispatcher pads: an undecided caller costs at most the floor, and an unpadded refusal could disclose.

### D3. The early decision is the release gate's own decision

The body of `annotate_page` gains a decide-only exit right after its withheld decision and the withheld receipt. `egress.page_withheld_entirely` runs that body with the exit. For a restricted caller, `op_get` calls it on the page that it just read, before history, links, the query log and the heat signal. If the page is withheld entirely, the read refuses with the missing-path text, and the receipt is the one that the gate records. Otherwise the exit records nothing, the read continues in the existing order, and `annotate_page` decides again and renders, so notices (L1 to L4) and releases are unchanged.

`release_level_for` was rejected for this step:

- It decides on the bytes on disk, not on the snapshot that the read holds.
- It skips the gate's collection-summary branch.
- Under a bound collection projection, it skips bridge admission.
- It records a receipt for every outcome and never records the page reference. The collector claims one receipt per path, outcome and level, so its receipt would replace the gate's own receipt for notices and releases too. The withheld receipt would lose its `ref`.

A call of the whole `annotate_page` as the early step was rejected too. It rendered and projected origin attribution twice, so a change that landed during the first, discarded pass reached the second pass as a change from before the read.

`quick_page_visible` was rejected because it also refuses L1 to L4, which would turn notices into missing pages.

The early decision costs a restricted read that is not refused one more release decision, without a second render.

### D4. Free the refusal's frames before the wait

The finished frames of a withheld read still hold the page that it read and decided. If the frames are freed after the wait, the refusal of a large page completes later than the refusal of a missing page, by a time that grows with the page. The dispatcher clears the finished frames of the exception and of its cause or context chain before it waits. The exception, its message and its chain are unchanged. Only the local variables of finished frames are released.

### D5. The floor value

F is 50 ms, provisionally. Its target is the smallest multiple of 5 ms that is at least twice the 99.9th percentile of restricted-refusal work on a withheld 1 MiB page (`MAX_GOVERNED_SEARCH_BYTES_PER_ITEM`) after D3, on the release runner profile. A change to F is a change to the `get-payload-shape` requirement.

## Risks

- A flood of restricted refusals holds one worker thread for up to F per refusal. `find` already pads its refusals to a longer class in the same way.
- On a loaded host, refusal work can run past F. Those refusals complete late. The `overrun` field shows how often.
