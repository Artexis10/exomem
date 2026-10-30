## ADDED Requirements

### Requirement: An import larger than the cell's volume arrives in windows

The Cloud CLI SHALL split a folder larger than the cell's window budget into windows. Each window SHALL be an ordinary `adopt` import with its own import ID and recipient. The CLI SHALL read the budget again right before it creates each window. It SHALL create the window only when that reading shows that the window fits:
- 2 × its text bytes + its artifact bytes is at most the window budget that the cell reports;
- when the cell publishes `import_limits`, used + `space_factor` × the window's bytes + `reserve_bytes` is at most the cell's current size, so admission grows no claim.

Windows SHALL carry offloadable artifacts first, resident artifacts next and text last, each in path order across the series. The CLI SHALL size each window within the reachable budget that the cell reports. While the budget does not cover the next window, each status call SHALL carry the series ID and that window's bytes as the demand. The CLI SHALL renew the demand at least every 5 minutes while it waits. The CLI SHALL wait and report in these cases:
- the budget is unknown;
- the reachable budget covers the next window, and the budget does not cover it yet;
- the reachable budget cannot hold the next member, and the reachable budget plus the job-pinned bytes can.

It SHALL stop and report, without waiting, in these cases:
- the cell reports media usage unknown or the allowance exhausted;
- the reachable budget plus the job-pinned bytes cannot hold even the next member. This stop SHALL say "band too small" and give the budget, the reachable budget, the job-pinned bytes and that member's bytes.

Every stop SHALL say that the members not yet sent are not in Exomem, and that the person must keep them.

#### Scenario: A folder of three times the budget lands without growth

- **WHEN** the CLI imports a folder of recordings and notes that holds three times the window budget, and the agent files each window
- **AND** no owner reads, downloads or hydrations run during the series
- **THEN** the CLI creates three or more windows, one at a time
- **AND** the cell's volume use stays at or below its growth threshold
- **AND** the cell's storage size does not change

#### Scenario: The volume has not drained

- **WHEN** the next window does not fit the budget that the cell reports, and the reachable budget covers it
- **THEN** the CLI creates no import, sends the window's bytes as the demand, waits, and reports the shortfall
- **AND** it creates the window once the reported budget covers it

#### Scenario: Another series used the room

- **WHEN** a budget reading covers the next window, and another series' window lands before the CLI creates it
- **THEN** the CLI reads the budget again right before the create, finds that the window no longer fits, and waits

#### Scenario: The budget is unknown

- **WHEN** the cell reports the window budget as unknown with a reason
- **THEN** the CLI creates no import, waits, and reports the reason

#### Scenario: Admission would grow the cell

- **WHEN** the cell publishes `space_factor` 2 and `reserve_bytes` of 0.25 GiB, its size is 20 GiB, it reports 2 GiB used and a budget of 9.75 GiB
- **THEN** the CLI sizes the next window to at most 8.875 GiB
- **AND** admission requests no growth for it

#### Scenario: The Job refuses a window for space

- **WHEN** the import Job refuses a window for lack of space
- **THEN** the CLI plans that window again from the current budget
- **AND** it sends no landed window again

#### Scenario: The allowance runs out during a series

- **WHEN** the cell reports the media allowance exhausted after five windows have landed
- **THEN** the CLI stops, reports the allowance and the members not yet sent, and creates no further window
- **AND** it says that those members are not in Exomem, and that the person must keep them

#### Scenario: Media usage is unknown

- **WHEN** the cell reports media usage unknown with the path of the note that makes it unknown
- **THEN** the CLI stops and reports that path, and creates no further window

#### Scenario: The band is too small

- **WHEN** the cell reports a reachable budget of 0.5 GiB and no job-pinned bytes, and the next member is a 0.8 GiB recording
- **THEN** the CLI stops with "band too small" and reports the budget, the reachable budget, the job-pinned bytes and the recording's bytes

#### Scenario: Long jobs pin the band

- **WHEN** the cell reports a reachable budget of 0.5 GiB and 6 GiB of job-pinned bytes
- **AND** the next member is a 0.8 GiB recording
- **THEN** the CLI waits and reports that claimed jobs keep originals on the volume
- **AND** it plans the next window once the reachable budget holds the recording

### Requirement: An older cell gets one import

The CLI SHALL treat a cell that reports no window budget as an older cell, or as a cell without the media store. When the cell publishes `import_limits` and the folder's declared bytes exceed `max_storage_gib`, the CLI SHALL refuse before any upload and report both figures. Otherwise it SHALL send the folder as one import, as before.

#### Scenario: An older cell and a folder within its size

- **WHEN** the cell reports no window budget and the folder fits within the cell's known size
- **THEN** the CLI creates one import for the folder

#### Scenario: An older cell and a folder above its size

- **WHEN** the cell reports no window budget, publishes `max_storage_gib` of 20, and the folder declares 30 GiB
- **THEN** the CLI refuses before any upload and reports both figures

### Requirement: The CLI refuses a series that cannot land, before any upload

Before the first upload, the CLI SHALL refuse a series in each of these cases and list the files concerned:
- 2 × its text bytes + its resident artifact bytes exceed the reachable budget that the cell reports;
- its offloadable bytes exceed the remaining media allowance that the cell reports.

#### Scenario: Resident files do not fit

- **WHEN** the cell reports a reachable budget of 8.75 GiB
- **AND** the series holds 3 GiB of text and 3 GiB of artifacts above the largest offloadable file
- **THEN** the CLI refuses the series before any upload
- **AND** it lists the resident files with their sizes and gives both figures

#### Scenario: A series larger than the remaining allowance

- **WHEN** the series' offloadable bytes exceed the remaining media allowance that the cell reports
- **THEN** the CLI refuses the series before any upload and reports both figures

### Requirement: A series resumes at its first unlanded window

The CLI SHALL record each series' partition: every member's path, size and modification time, and each window's state. A rerun SHALL check the cell's import receipts before it skips a window that the record calls landed. It SHALL resume at the first window that has no receipt, and SHALL send no window that has one again.

#### Scenario: A restart resumes at the third window

- **WHEN** the CLI stops after window 2 lands, and the person imports the same folder again
- **THEN** the CLI creates window 3
- **AND** it sends no byte of windows 1 and 2

#### Scenario: A cell restore lost a landed window

- **WHEN** the record calls window 2 landed, but the cell has no receipt for it after a restore from backup
- **THEN** the CLI sends window 2 again before window 3

### Requirement: The CLI and the cell classify members by one rule

One module SHALL hold the rule that classifies a member as text, an offloadable artifact or a resident artifact. The CLI and the adoption inventory SHALL import it, and the cell SHALL report its inputs. An artifact SHALL be offloadable when three bounds hold. Its `age` ciphertext size is at most the store's object bound and at most the hydration capacity. Its plaintext size is at most the volume fraction × the volume size. The volume fraction is a store-policy value, 0.25 by default. Every other artifact SHALL be resident.

#### Scenario: A video within the three bounds

- **WHEN** the object bound is 5 GiB plus 2 MiB, and the hydration capacity is 5.25 GiB
- **AND** the volume is 20 GiB, and a video is 4.5 GiB
- **THEN** the video is offloadable

#### Scenario: A file of exactly the object bound

- **WHEN** the object bound is 5 GiB plus 2 MiB, and a file is exactly that size
- **THEN** its `age` ciphertext is larger than the bound, and the file is resident

#### Scenario: A small hydration capacity lowers the limit

- **WHEN** the object bound is 5 GiB plus 2 MiB, and the hydration capacity is 3 GiB
- **AND** the volume is 20 GiB, and a video is 4.5 GiB
- **THEN** the video is resident, because its ciphertext needs more than 3 GiB
- **AND** the cell reports a largest offloadable file just under 3 GiB

#### Scenario: A small volume lowers the limit

- **WHEN** the object bound is 5 GiB plus 2 MiB, and the hydration capacity is 5.25 GiB
- **AND** the volume is 12 GiB, and a video is 4 GiB
- **THEN** the video is resident, and the cell reports a largest offloadable file of 3 GiB

#### Scenario: The CLI and the inventory agree

- **WHEN** a generated folder holds recordings, notes, a text file above the inline text limit and a file above the object bound
- **THEN** the CLI's class for each member equals the adoption inventory's class for it

### Requirement: The cell reports its window budget

The Adoption Studio status SHALL report these figures. Each figure that the cell cannot read SHALL be unknown with a reason, never zero.
- The window budget: growth threshold × volume size − used, less the fixed terms and the text bytes that landed windows have not filed. The fixed terms are the free-space reserve, the hydration capacity and `max_hydrations` × the largest offloadable file.
- The reachable budget: the window budget once the agent files every landed window, pending offloads finish and eviction frees every eligible original. It SHALL leave out an original that an owner read within the minimum idle age.
- The job-pinned bytes: the offloadable originals that claimed, unfinished media jobs keep on the volume, the copies that the worker hydrated for them included.
- The inputs of the classification rule: the object bound, the hydration capacity, the volume fraction, the volume size and the inline text limit, and the largest offloadable file that they allow.
- The remaining media allowance.
- The drain state: the unfiled bytes of landed windows, the count of pending offloads and the evictable bytes.
- For each landed window, the count and bytes of its unfiled members. An unfiled member is a manifest member, other than the series file, that no adoption run has filed. A window with no run SHALL count every member except the series file.
- For each landed window, the count and bytes of its residual, and its member paths page by page.
- For each discarded window whose residual record lists a member, the record's members as "discarded, not filed", with path, bytes and code, page by page.

A status call MAY carry a series ID and the next window's bytes. The cell SHALL then record them as that series' demand for eviction, or renew it, and keep the other series' demands. It SHALL ignore a demand above the reachable budget. A cell without the media store SHALL report no window budget.

#### Scenario: The budget is computed from its inputs

- **WHEN** the volume is 20 GiB, the growth threshold is 80%, use is 3 GiB, and the reserve is 0.25 GiB
- **AND** the hydration capacity is 5.25 GiB, the placed-copy allowance is 5 GiB, and landed windows hold 0.5 GiB of unfiled text
- **THEN** the status reports a window budget of 2 GiB

#### Scenario: The reachable budget counts what eviction can free

- **WHEN** the window budget is 1 GiB, 6 GiB of eligible originals are cached, and landed windows hold 2 GiB of unfiled offloadable members
- **AND** an owner read another 1 GiB of originals within the minimum idle age
- **THEN** the status reports a reachable budget of 9 GiB

#### Scenario: A landed window with no run

- **WHEN** a window has landed with 40 members and the series file, and no adoption run has started on its stage
- **THEN** the status reports 40 unfiled members for that window, with their bytes

#### Scenario: A status call records a demand

- **WHEN** a status call carries a series ID and 4 GiB as the next window's bytes
- **AND** the window budget is 1 GiB, and 6 GiB of originals are eligible
- **THEN** the cell records a demand of 4 GiB for that series, and eviction runs until the budget covers it

#### Scenario: An input cannot be read

- **WHEN** the cell cannot read the volume's free space
- **THEN** the status reports the window budget as unknown with a reason, not as zero

#### Scenario: A cell without the store

- **WHEN** a cell's media store is off
- **THEN** the status reports no window budget, and the CLI follows the older-cell rule

### Requirement: The import receipt records each member's identity

For an `adopt` import, the import Job SHALL record each member's identity in the member manifest, beside its path, size and SHA-256. The identity SHALL be the staged file's inode, size, `mtime_ns` and `ctime_ns`, read after the Job sets the file's modification time. It SHALL NOT include the device number, because each pod can see the volume under a different device number.

#### Scenario: The receipt carries the identity

- **WHEN** an `adopt` import finishes and the runtime starts again on the same volume, under a different device number
- **THEN** each manifest entry's identity equals the stat of its staged file

### Requirement: A discard keeps a residual record

When the discard Job discards a recorded stage, it SHALL keep a residual record in the window's receipt. The record SHALL list each staged member that adoption did not file, with its path, bytes and residual code. It SHALL leave out the series file. The Job SHALL write the record, empty or not, in the same receipt write that drops the member manifest. A retried discard SHALL keep the record that an earlier attempt wrote. When the stage exists and the Job cannot read its receipt, the discard SHALL refuse with `IMPORT_RECEIPT_UNREADABLE` and keep the stage. The refusal SHALL name its exit: `import_discard` with `walk_stage` set. That discard SHALL walk the stage and record each staged file except the series file, with its path, bytes and the code `receipt_unreadable`. It SHALL write that record to a separate file for the import ID, leave the unreadable receipt in place, and then remove the stage. A retried walk SHALL keep the record that an earlier walk wrote. The status, a resend and an acknowledgement SHALL treat that record as the window's residual record. A record SHALL stay until a resend or an acknowledgement clears its members.

#### Scenario: A discard after a path conflict

- **WHEN** the person discards a window in which two members stayed unfiled after a path conflict
- **THEN** the receipt keeps a residual record of both members, with their paths, bytes and the conflict code
- **AND** the stage folder is gone

#### Scenario: A discard stops and runs again

- **WHEN** a discard stops after it rewrote the receipt and before it removed the stage, and the person discards the window again
- **THEN** the receipt still holds the residual record from the first attempt

#### Scenario: The receipt cannot be read

- **WHEN** the person discards a window whose stage exists, and its receipt cannot be read
- **THEN** the discard refuses with `IMPORT_RECEIPT_UNREADABLE`, names the `walk_stage` exit, and the stage stays

#### Scenario: A discard walks the stage

- **WHEN** an agent discards that window again with `walk_stage` set
- **THEN** a separate record lists each staged file except the series file, with the code `receipt_unreadable`
- **AND** the unreadable receipt stays, the stage folder is gone, and the status lists those files as "discarded, not filed"

### Requirement: Each window carries the series file

The CLI SHALL record the full path list of a series and its label, the source folder's name. It SHALL put both in each window as one file at the stage root, named `.exomem-series.json`. The import Job SHALL stage that file like any member. The CLI SHALL refuse, before any upload, a source folder that holds a file of that name at its top level.

#### Scenario: A later window carries the whole list

- **WHEN** the CLI sends window 4 of a series of 9 windows
- **THEN** the stage of window 4 holds the series file, and it lists the paths of all 9 windows and the series label

#### Scenario: The source folder holds the reserved name

- **WHEN** the source folder holds `.exomem-series.json` at its top level, and the person includes hidden files
- **THEN** the CLI refuses the folder before any upload and names the file

### Requirement: The CLI resends the residual as a new series

When the person asks to resend a series' residual, the CLI SHALL send the residual members as a new series with new import IDs. It SHALL take their paths from the residual records of the series' discarded windows. It SHALL resend no member of a window that is not discarded yet. Each resend window SHALL carry the first series' file unchanged. The CLI SHALL refuse a member whose source file differs in size or modification time from the series record. After a resend window lands, the CLI SHALL clear its members from the old residual records.

#### Scenario: A restore sent large members to the residual

- **WHEN** two 600 MiB members of window 3 went to the residual after a backup restore, and window 3 is discarded
- **AND** the person asks to resend the residual
- **THEN** the CLI reads the two members from window 3's residual record and sends them as a new series
- **AND** adoption files them by rename under the first series' collection root and label
- **AND** status stops listing them as "discarded, not filed" once the resend window lands

#### Scenario: A window not yet discarded

- **WHEN** the person asks to resend the residual, and window 5 still has a residual and is not discarded
- **THEN** the CLI sends no member of window 5, and reports that the window must be filed or discarded first

#### Scenario: A residual member changed at the source

- **WHEN** a residual member's source file has a newer modification time than the series record
- **THEN** the CLI does not send that member and reports it

### Requirement: The CLI tells the person to keep the source

When the last window lands, the CLI SHALL report that the upload is done and that filing continues. It SHALL tell the person to keep the source folder while the cell reports an unfiled member in a landed window. It SHALL also do so while a residual record lists a member. The series file is never an unfiled member, and a residual record that lists no member does not count.

#### Scenario: The last window lands before filing ends

- **WHEN** the last window lands while earlier windows still have unfiled members
- **THEN** the CLI reports the upload as done, names the windows with unfiled members, and tells the person to keep the source folder
