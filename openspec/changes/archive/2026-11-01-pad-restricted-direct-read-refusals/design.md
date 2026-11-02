## Context

The dispatcher `writer_lease.invoke_command` is the one entry that MCP, REST, hosted cells and the CLI share, and each surface binds its principal before it calls the dispatcher. `projection_timing` already pads `find` and `ask_memory` into registered completion classes, and each class has a deadline that refuses a request that runs past it. `egress.caller_restricted` answers whether anything, RAW included, could be withheld from the bound caller. Every error that leaves the dispatcher passes `egress.postfilter_error`, whose reference gate replaces any reference to a withheld artifact in the error text.

## Decisions

### D1. A pad-only floor, not a registered completion class

The floor is one constant beside the existing classes, with a helper that waits and never raises. A registered class was rejected: its deadline raises a refusal when the request runs long, so a slow withheld read would refuse with a different answer than a fast missing one, and the overrun itself would become the signal. A slow read therefore completes late with its own refusal. The `command.refusal_floor` span records the overrun for the owner, so overruns are visible without being visible to the caller.

### D2. The dispatcher asks about restriction only after a refusal

The dispatcher calls `caller_restricted` only when a marked command raised, so its floor costs a success nothing. If the question itself fails, the dispatcher pads: an undecided caller costs at most the floor, and an unpadded refusal could disclose.

The read itself asks the same question once, before it renders the page, because D3 needs the answer before the optional work. For an owner in file mode, the question returns before any policy work.

### D3. The early decision is the release gate's own decision

The body of `annotate_page` gains a decide-only exit right after its withheld decision and the withheld receipt. `egress.page_withheld_entirely` runs that body with the exit. For a restricted caller, `op_get` calls it on the page that it just read, before history, links, the query log and the heat check. If the page is withheld entirely, the read refuses with the missing-path text, and the receipt is the one that the gate records. Otherwise the exit records nothing, the read continues in the existing order, and `annotate_page` decides again and renders, so notices (L1 to L4) and releases are unchanged.

`release_level_for` was rejected for this step:

- It decides on the bytes on disk, not on the snapshot that the read holds.
- It skips the gate's collection-summary branch.
- Under a bound collection projection, it skips bridge admission.
- It records a receipt for every outcome and never records the page reference. The collector claims one receipt per path, outcome and level, so its receipt would replace the gate's own receipt for notices and releases too. The withheld receipt would lose its `ref`.

A call of the whole `annotate_page` as the early step was rejected too. It rendered and projected origin attribution twice, so a change that landed during the first, discarded pass reached the second pass as a change from before the read.

`quick_page_visible` was rejected because it also refuses L1 to L4, which would turn notices into missing pages.

The early decision costs a restricted read that is not refused one more release decision. An ordinary page is not rendered by it. A collection's generated summary page is, because one step renders the summary and decides its release.

The heat signal already left out pages withheld from the caller. The early refusal only skips that check. The query log did record a refused withheld read as a successful `get`, and the early refusal removes that row.

### D4. Free the refusal's frames before the wait

The finished frames of a withheld read still hold the page that it read and decided. If the frames are freed after the wait, the refusal of a large page completes later than the refusal of a missing page, by a time that grows with the page. The dispatcher clears the finished frames of the exception and of its cause or context chain before the wait. The exception, its message and its chain are unchanged. Only the local variables of finished frames are released.

### D5. The surface waits, with an `await`, after its own handling

A refusal leaves the dispatcher as an exception, and the MCP surface then handles it: FastMCP logs it, converts it into a tool error, and the call trace and the authorization re-check run. That handling took different times for a withheld and a missing refusal, so a floor that ended inside the dispatcher left the difference visible.

Two shapes were compared. One normalises the refusal to one exception type, message and traceback at the floor. The other moves the wait to the surface. The surface wait was chosen. It covers all handling after the dispatcher, not only the exception's shape, and an `await` frees the worker thread that a wait on the dispatcher's thread holds for each refusal. Since the command surface answers a coded refusal with the error envelope, FastMCP neither logs nor converts it, so the exception's shape no longer changes the time of anything inside the floor, and it needs no normalising.

The dispatcher marks the request's floor as pending, with its own start time, in a context variable that the outermost MCP middleware and the REST handler open. The surface then waits until the floor has passed since the dispatcher started, after everything it does with the refusal. When no surface opened the floor, as on the CLI, the dispatcher's thread waits, and the last 2 ms of that wait spin so that it ends on time.

`asyncio.sleep` alone did not end on time. The event loop rounds its poll timeout up to whole milliseconds, so the wait woke up to 2 ms late, by an amount that follows the fraction of a millisecond left in the wait. A withheld and a missing refusal take steady but different work times, so their wakes differed by about 1 ms on every call. The surface wait therefore ends 3 ms early on the loop and sleeps the rest on a worker thread, whose sleep keeps time. Nothing spins on the loop, and nothing spins on that thread, because a spinning thread would hold the interpreter lock that the loop needs.

### D6. No logging filter

An earlier round removed the traceback from FastMCP's log record of a coded refusal, because rendering it took longer than the refusal and differed between a withheld and a missing one. The command surface now answers every coded refusal with the error envelope, so FastMCP no longer logs a read refusal at all, and the filter had nothing left to change. It was removed. An exception without a code is a fault and keeps its traceback, as before.

### D7. The reference gate keeps its census per vault

The gate resolves references against every file in the vault. It walked the whole vault for every error and built one alternation pattern of every file alias for every text it cleaned. Building that pattern's source ran on every call, and compiling it ran whenever the set changed. Both grew with the vault, so every refused or failed call paid seconds on a large vault.

The census now lives per vault in `governance/artifact_census`, as one listing per directory with a stamp of that directory's device, inode, modification time and change time. Creating, removing or renaming an entry changes its directory's stamp. On Linux and macOS, a tool that restores an old modification time still moves the change time. On Windows the change time is the creation time, so there only the modification time covers such a tool. A walk lists again only the directories whose stamp changed. A directory stamped within 2 s of the walk is listed again on the next walk too, because a second change inside the same timestamp tick would leave the stamp unchanged. The alias tables depend only on file names, so a walk that finds the same names keeps them. A write that replaces a file in place therefore costs one directory listing.

The literal pass now looks candidate spans up in a set instead of matching one pattern of all aliases. A candidate starts where no path character precedes it and ends where none follows, as the pattern's look-behind and look-ahead required. The longest alias wins at each start, and the scan resumes after a match. Case keys reproduce `re.IGNORECASE`: each character maps to one member of its case class, from its simple lowercase and the standard library's table of extra case pairs. A prefix set stops the scan as soon as no alias can continue. A randomised comparison with the former pattern, case variants and the one case class that mixes path and non-path characters included, found no difference in matches.

### D8. One policy for one read

One direct read takes several release decisions, and each loaded and re-checked the policy. The read now opens a request scope in which the first load answers every later load. A read changes no governance, so it decides as if the read had been served a moment earlier. Only a read opens this scope; a request that changes governance must see its own change.

### D9. The floor value

F is 55 ms: the smallest multiple of 5 ms that is at least twice the 99.9th percentile of restricted-refusal work on a withheld 1 MiB page (`MAX_GOVERNED_SEARCH_BYTES_PER_ITEM`). With D7 and D8, that work no longer grows with the vault: its median and its 99.9th percentile were about the same at 300, 1,500, 3,000 and 15,000 files. Its 99.9th percentile was 27 ms at 3,000 files, on one developer machine under load. A change to F is a change to the `get-payload-shape` requirement.

## Risks

- On a loaded host, refusal work can run past F. Those refusals complete late. The `overrun` field shows how often, and `work_ms` shows by how much.
- The first refusal after a file name changed rebuilds the census, and that work grows with the vault. It can overrun F. The rebuild does not depend on the page that the refusal concerns.
- The census keeps the vault's file names in memory, about 40 MiB for 15,000 files.
- A filesystem that does not update a directory's modification time when its entries change would leave the census stale. Local filesystems on Linux, macOS and Windows update it.
- A dangling file symlink whose target is created later changes no directory stamp, so the census does not see the target until its directory changes. The former per-request walk saw it.
- The first withheld refusal in a process builds the memory-reference index, and that work grows with the vault. A missing refusal does not build it. Later refusals reuse the index.
