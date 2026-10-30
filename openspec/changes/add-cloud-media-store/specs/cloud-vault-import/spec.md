## ADDED Requirements

### Requirement: An import larger than the cell's volume arrives in windows

The Cloud CLI SHALL split a folder larger than the cell's window budget into windows. Each window SHALL be an ordinary `adopt` import with its own import ID and recipient. The CLI SHALL create a window only when it fits:
- 2 × its text bytes + its artifact bytes is at most the window budget that the cell reports;
- when the cell publishes `import_limits`, used + `space_factor` × the window's bytes + `reserve_bytes` is at most the cell's current size, so admission grows no claim.

Windows SHALL carry offloadable artifacts first, resident artifacts next and text last, each in path order across the series. The CLI SHALL size each window within the reachable budget that the cell reports. While the budget does not cover the next window, each status call SHALL carry that window's bytes as the demand. The CLI SHALL wait and report in these cases:
- the budget is unknown;
- the reachable budget covers the next window, and the budget does not cover it yet.

It SHALL stop and report, without waiting, in these cases:
- the cell reports media usage unknown or the allowance exhausted;
- the reachable budget cannot hold even the next member. This stop SHALL say "band too small" and give the budget, the reachable budget and that member's bytes.

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

#### Scenario: Media usage is unknown

- **WHEN** the cell reports media usage unknown with the path of the note that makes it unknown
- **THEN** the CLI stops and reports that path, and creates no further window

#### Scenario: The band is too small

- **WHEN** the cell reports a reachable budget of 0.5 GiB, and the next member is a 0.8 GiB recording
- **THEN** the CLI stops with "band too small" and reports the budget, the reachable budget and the recording's bytes

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

One module SHALL hold the rule that classifies a member as text, an offloadable artifact or a resident artifact. The CLI and the adoption inventory SHALL import it, and the cell SHALL report its inputs. An artifact SHALL be offloadable when two bounds hold. Its `age` ciphertext size is at most the store's object bound, and its plaintext size + ciphertext size is at most the hydration capacity. Every other artifact SHALL be resident.

#### Scenario: A video within both bounds

- **WHEN** the object bound is 1 GiB, the hydration capacity is 2.25 GiB, and a video is 900 MiB
- **THEN** the video is offloadable

#### Scenario: A file of exactly the object bound

- **WHEN** the object bound is 1 GiB and a file is exactly 1 GiB
- **THEN** its `age` ciphertext is larger than the bound, and the file is resident

#### Scenario: A small hydration capacity lowers the limit

- **WHEN** the object bound is 1 GiB, the hydration capacity is 1.5 GiB, and a video is 900 MiB
- **THEN** the video is resident, because its hydration needs more than 1.5 GiB
- **AND** the cell reports a largest offloadable file just under 768 MiB

#### Scenario: The CLI and the inventory agree

- **WHEN** a generated folder holds recordings, notes, a text file above the inline text limit and a file above the object bound
- **THEN** the CLI's class for each member equals the adoption inventory's class for it

### Requirement: The cell reports its window budget

The Adoption Studio status SHALL report these figures. Each figure that the cell cannot read SHALL be unknown with a reason, never zero.
- The window budget: growth threshold × volume size − used, less the fixed terms and the text bytes that landed windows have not filed. The fixed terms are the free-space reserve, the hydration capacity, `max_hydrations` × the largest offloadable file, and one offload temporary file of up to the object bound.
- The reachable budget: the window budget once the agent files every landed window, pending offloads finish and eviction frees every eligible original. It SHALL leave out an original that an owner read within the minimum idle age.
- The inputs of the classification rule: the object bound, the hydration capacity and the inline text limit, and the largest offloadable file that they allow.
- The remaining media allowance.
- The drain state: the unfiled bytes of landed windows, the count of pending offloads and the evictable bytes.
- For each landed window, the count and bytes of its residual, and its member paths page by page.

A status call MAY carry the next window's bytes. The cell SHALL then record them as the series' demand for eviction, or renew the demand. A cell without the media store SHALL report no window budget.

#### Scenario: The budget is computed from its inputs

- **WHEN** the volume is 20 GiB, the growth threshold is 80%, use is 7 GiB, the reserve is 0.25 GiB and the hydration capacity is 2.25 GiB
- **AND** the placed-copy allowance is 1 GiB, the offload temporary allowance is 1 GiB, and landed windows hold 0.5 GiB of unfiled text
- **THEN** the status reports a window budget of 4 GiB

#### Scenario: The reachable budget counts what eviction can free

- **WHEN** the window budget is 1 GiB, 6 GiB of eligible originals are cached, and landed windows hold 2 GiB of unfiled offloadable members
- **AND** an owner read another 1 GiB of originals within the minimum idle age
- **THEN** the status reports a reachable budget of 9 GiB

#### Scenario: A status call records a demand

- **WHEN** a status call carries 4 GiB as the next window's bytes, and the window budget is 1 GiB
- **THEN** the cell records a demand of 4 GiB, and eviction runs until the budget covers it

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

### Requirement: Each window carries the series file

The CLI SHALL record the full path list of a series and its label, the source folder's name. It SHALL put both in each window as one file at the stage root, named `.exomem-series.json`. The import Job SHALL stage that file like any member. The CLI SHALL refuse, before any upload, a source folder that holds a file of that name at its top level.

#### Scenario: A later window carries the whole list

- **WHEN** the CLI sends window 4 of a series of 9 windows
- **THEN** the stage of window 4 holds the series file, and it lists the paths of all 9 windows and the series label

#### Scenario: The source folder holds the reserved name

- **WHEN** the source folder holds `.exomem-series.json` at its top level, and the person includes hidden files
- **THEN** the CLI refuses the folder before any upload and names the file

### Requirement: The CLI resends the residual as a new series

When the person asks to resend a series' residual, the CLI SHALL send the residual members as a new series with new import IDs. It SHALL take their paths from the residual that the cell lists for each window, before the window is discarded. It SHALL refuse a member whose source file differs in size or modification time from the series record.

#### Scenario: A restore sent large members to the residual

- **WHEN** two 600 MiB members of window 3 went to the residual after a backup restore, and the person asks to resend the residual
- **THEN** the CLI sends the two members as a new series, and adoption files them by rename

#### Scenario: A residual member changed at the source

- **WHEN** a residual member's source file has a newer modification time than the series record
- **THEN** the CLI does not send that member and reports it

### Requirement: The CLI tells the person to keep the source

When the last window lands, the CLI SHALL report that the upload is done and that filing continues. It SHALL tell the person to keep the source folder until the cell reports an empty residual for every window.

#### Scenario: The last window lands before filing ends

- **WHEN** the last window lands while earlier windows still have unfiled members
- **THEN** the CLI reports the upload as done, names the windows with a residual, and tells the person to keep the source folder
