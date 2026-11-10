# Design: upload folders over MCP

## Context

`upload_files` mints a ticket bound to the calling session. A POST to `/upload` with the ticket and `hold=1` stores the bytes as a held upload, redeemable once by the same session within an hour, up to 100 MiB. `imports.file` files a staged folder `_Imports/<id>` page by page. `exomem import` stages a folder by copying it and writing a stage record that lets adoption remove the staged copies after filing. `local-client-ingress` requires that no tool argument names a local path.

## Goals

- A client with no shell imports a folder in three steps: pack, send, repeat one call.
- An archive that is unsafe, too large or unreadable stages nothing, and the agent learns which member broke which limit.
- A retried call never unpacks twice and never files twice.

## Non-goals

- A new listed tool or door operation.
- Cloud, a browser upload page and ChatGPT archive pulls.
- Changing the 100 MiB hold cap or the ticket's life.

## Decisions

### D1. The route reuses the held upload, and `imports.file` is the one redeemer

An import archive is a held upload of lane `import`, redeemed by `imports.file` and by nothing else. The lane is an additional value of the existing `lane` field, because a second boolean field costs more schema than the budget leaves, and the lane already names where the bytes go. The new lane joins the handoff lanes but not the bearer lanes of `UPLOAD_LANES`, so the bearer routes and `exomem attach` keep sending only sources and evidence.

The agent sends bytes, never a path, so `local-client-ingress` holds: no tool argument names a local path. `upload` is the ticket the server issued, and the server derives every path from it.

The receiver accepts multipart POST only, so the `next` step names a ready `curl -F` line. A `PUT` would need a gateway change, and Cloud is out of scope.

### D2. The stage derives from the ticket, so a retried call unpacks once

The hold's secret and the stage id both derive from the ticket's nonce, with a distinct tag for each. A repeated first call finds `_Imports/<id>` and returns it. A repeat after filing removed the stage finds a run of that path and returns the path, so `file` answers `done` as it does for a path alone. The id is shaped as a UUID, as the ids of `exomem import` are.

One lock covers every unpack of a vault, so a retry that overlaps a slow first call waits for it. The unpack writes into a hidden `_Imports/.<id>.unpack` and renames it, so the stage exists only whole. A leftover hidden folder is a crashed call, which the lock proves is not running, and the next call removes it. The stage record is written last, because it lets adoption remove staged files: a crash before it leaves a copy, never a loss.

### D3. Each limit has one reason

| Limit | Reason |
| --- | --- |
| Links, hardlinks, devices and sparse entries refused | A link written into the stage could point outside it, and a device is not a file. The set is fixed by the archive formats. |
| Absolute and `..` paths, and any path that resolves outside the stage, refused | They write outside `_Imports`. |
| 65,536 entries | The cap that the archive preserver already holds, so one archive cannot make the inventory unbounded. |
| `EXOMEM_IMPORT_MAX_EXPANDED_BYTES`, default 1 GiB | The held archive is at most 100 MiB, which a compressed archive can expand a thousandfold. The cap bounds that bomb to what one import may stage. It counts the bytes written, not the sizes the archive declares. |
| Free space against the existing import reserve | A full volume stops the import as `file` does for a chunk, with `filing: stopped` and the hold kept, so the agent repeats the call once there is room. |
| Zip case-alias duplicates and encrypted entries refused | A case-insensitive volume would merge the aliases, and an encrypted entry cannot be read. These are the archive preserver's member rules. |

A refusal names the member, JSON-escaped and cut to 120 characters, because the name is the sender's content. A refusal for a reason that cannot pass, such as an unsafe path, releases the hold, since the same bytes would be refused again and would otherwise occupy hold quota. A refusal that may pass, such as free space, keeps the hold.

### D4. Known limits

- A tar's total is counted from the member headers the unpacker has already accepted, which equal the bytes written because it copies exactly the declared size or fails. A gzip stream that follows the end-of-archive block with zeros costs CPU and no disk.
- The hold is released when the stage is on disk, so a crash between the rename and the release leaves a hold that expires within the hour; the next call finds the stage and unpacks nothing.
