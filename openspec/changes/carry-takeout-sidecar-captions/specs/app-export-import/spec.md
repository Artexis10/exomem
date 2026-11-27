## MODIFIED Requirements

### Requirement: A registry declares each recognised export format

The `source-formats` registry SHALL declare each recognised format as a shipped pack with a vault overlay. Each entry SHALL carry `file_glob`, a closed `parser` value that code implements, and the facts and words its parser needs: `item_kind` for a parser that derives items, `item_keys`, `root`, `label_header`, `skip_labels`, the naming facts of a sidecar parser, `roles`, `untitled`, `attachment` and `words`. An entry lacking a fact its parser needs, or naming an unimplemented `parser`, SHALL be refused. An overlay MAY retire an entry or change its words and `skip_labels`; a shipped entry's recognition facts SHALL stay fixed.

#### Scenario: A vault retires a format

- **WHEN** the vault overlay retires the ChatGPT entry
- **THEN** a ChatGPT export files as one ordinary file and nothing is derived from it

#### Scenario: An overlay names an unknown parser

- **WHEN** a vault overlay adds an entry whose `parser` is not implemented
- **THEN** the registry refuses that entry and keeps the others

### Requirement: An export is recognised by its name pattern and its items' keys

A staged member SHALL be recognised only when its name matches an entry's `file_glob` and its content matches what the entry's parser reads: a JSON array whose first item carries every key in `item_keys`, an XML document whose root element is `root`, an mbox whose first line starts with `From `, or a JSON object of at most 1 MiB that carries every key in `item_keys`. A file that an expansion wrote SHALL never be recognised. Code SHALL branch only on `parser`.

#### Scenario: Two vendors share a file name

- **WHEN** a stage holds a `conversations.json` whose first item carries `mapping` and `current_node`, and another whose first item carries `chat_messages` and `uuid`
- **THEN** the first is recognised as a ChatGPT export and the second as a Claude export

#### Scenario: An unrelated file with the same name

- **WHEN** a stage holds a `conversations.json` whose items match no entry's `item_keys`
- **THEN** it is not recognised and files as an ordinary staged file

#### Scenario: A ChatGPT export in numbered shards

- **WHEN** a stage holds `conversations-000.json` and `conversations-001.json`, each with ChatGPT items
- **THEN** both are recognised, each expands into its own transcripts, and each files once as an artifact Source

#### Scenario: An export inside an export is not expanded

- **WHEN** a mail message carries an attachment named `archive.mbox`
- **THEN** the attachment files as an ordinary file and nothing is derived from it

#### Scenario: An album's metadata file

- **WHEN** a Takeout album folder holds a `metadata.json` without `photoTakenTime`
- **THEN** it is not recognised and files as an ordinary staged file

### Requirement: Filing files each export item as its own Source

At the start of `file`, before selection, the cell SHALL expand every recognised export in the stage into one Markdown transcript per item, named by the vendor's item id or, where the export has none, by a hash of the item. It SHALL record each with the item's created time, Source kind and export path. Each transcript SHALL then be planned, deduplicated, applied and counted like any staged file. The export file itself SHALL file once, as an artifact Source whatever its size. A recognised photo sidecar is not an export: it SHALL derive nothing and file nothing.

#### Scenario: A ChatGPT export becomes one Source per conversation

- **WHEN** a person drops a ChatGPT export that holds 40 conversations on Home and the cell files it
- **THEN** the vault gains 40 session Sources, each titled with its conversation's title and each carrying the conversation's created time, plus one Source for the export file

#### Scenario: The same export is imported again

- **WHEN** the same export is imported again unchanged
- **THEN** every transcript is skipped as already imported, and no duplicate Source is created

## ADDED Requirements

### Requirement: A Takeout sidecar pairs only one to one

A recognised Takeout sidecar SHALL pair with an image or video in its own staged folder only when its name and its `title` both fit that file under the entry's naming facts, no other file fits it, and that file fits no other sidecar. A title's extension fits a file's extension only when both have the same MIME type, taken from the standard library's built-in table. A pair SHALL be recorded without any position.

#### Scenario: Duplicates with the copy number in either place

- **WHEN** a folder holds `IMG.jpg`, `IMG(1).jpg`, `IMG(2).jpg`, `IMG.jpg.json`, `IMG.jpg(1).json` and `IMG(2).jpg.supplemental-metadata.json`, every sidecar titled `IMG.jpg`
- **THEN** each sidecar pairs with its own photo, and no photo receives another's caption

#### Scenario: A long name cut by Takeout

- **WHEN** a photo's name and its sidecar's name are both cut at the lengths the entry names, and the sidecar's `title` holds the full name
- **THEN** the sidecar pairs with that photo

#### Scenario: Two files of one shot

- **WHEN** a folder holds `IMG_60.heic`, `IMG_60.jpg` and one sidecar for each, titled with the file's own name
- **THEN** each sidecar pairs with its own file
- **AND** the same holds for a raw file and its JPG

#### Scenario: A sidecar for another format

- **WHEN** a folder holds `Scan_62.png` and a sidecar titled `Scan_62.jpg`
- **THEN** the sidecar stays unpaired, and the PNG files without its caption

#### Scenario: A name form that is not read

- **WHEN** a sidecar's title and name carry no extension where the file's name carries one, or a sidecar titled `PXL_1.MP` meets the file `PXL_1.MP.jpg`
- **THEN** the sidecar stays unpaired and is counted with the reason `no-photo`

#### Scenario: Two sidecars fit one photo

- **WHEN** two sidecars in one folder both fit the same photo
- **THEN** neither pairs, and both are counted unpaired

### Requirement: A Takeout sidecar is counted by its outcome, never filed

A recognised sidecar SHALL never file as a Source. When filing reaches done, the filing view SHALL count each sidecar in `left_out` under exactly one reason, from its photo's filing result, and SHALL NOT add it to the stage total as a derived file. The reasons are `IMPORT_SIDECAR_APPLIED`, `IMPORT_SIDECAR_EMPTY`, `IMPORT_SIDECAR_PHOTO_ALREADY_IMPORTED`, `IMPORT_SIDECAR_PHOTO_NOT_FILED`, `IMPORT_SIDECAR_UNPAIRED` and `IMPORT_SIDECAR_UNREADABLE`, which the view lists by path.

#### Scenario: A Takeout album

- **WHEN** a staged folder holds 50 photos and their 50 sidecars, 24 of which carry a caption
- **THEN** 50 photo Sources file and no JSON Source does, and the filing view counts 24 `IMPORT_SIDECAR_APPLIED` and 26 `IMPORT_SIDECAR_EMPTY`, and reads done

#### Scenario: A sidecar without its photo

- **WHEN** a sidecar's photo is in another archive part that was not staged
- **THEN** the sidecar files nothing, and the filing view counts and lists it as `IMPORT_SIDECAR_UNPAIRED`

### Requirement: A sidecar's reason says what became of its photo

`IMPORT_SIDECAR_APPLIED` and `IMPORT_SIDECAR_EMPTY` SHALL mean that the sidecar was paired with a photo that filed now: `APPLIED` that its caption went to the photo, and `EMPTY` that it had no caption to give. `IMPORT_SIDECAR_PHOTO_ALREADY_IMPORTED` SHALL mean that the vault already held the photo, so the sidecar's caption was not applied: its reason text SHALL say so, and it counts as filed. `IMPORT_SIDECAR_PHOTO_NOT_FILED` SHALL mean that the photo did not file, which the person reviews.

#### Scenario: The same Takeout imported again

- **WHEN** the same Takeout is imported again unchanged
- **THEN** every photo is skipped as already imported, no JSON Source files, and every sidecar counts as `IMPORT_SIDECAR_PHOTO_ALREADY_IMPORTED`, with `captioned` at 0
- **AND** the reply says that the sidecars' captions were not applied, because the vault already held the photos

#### Scenario: A photo in two albums

- **WHEN** a Takeout holds the same photo in two albums, each with its own sidecar
- **THEN** one photo Source files, with the caption of its own sidecar, and the other album's sidecar counts as `IMPORT_SIDECAR_PHOTO_ALREADY_IMPORTED`
- **AND** nothing in the stage's report needs the person's review

#### Scenario: A photo that did not file

- **WHEN** a paired photo is larger than the adoption ceiling and does not file
- **THEN** its sidecar counts as `IMPORT_SIDECAR_PHOTO_NOT_FILED`, which the report lists as needing review

### Requirement: The view counts the captions that sidecars gave

Over the sidecars whose photo filed now, the filing view SHALL count those that carry a caption (`captioned`). It SHALL count no time, because no sidecar time is applied.

#### Scenario: Counts over an album

- **WHEN** a staged folder holds 50 photos that each have a sidecar, 24 of them with a caption
- **THEN** the filing view counts `captioned` 24

### Requirement: Photos wait for their sidecars

The images and videos of a stage that holds recognised sidecars SHALL stay out of selection until the pass over the stage's sidecars ends: when every JSON file is examined and the pairs are decided. A JSON file that cannot be read SHALL get its own record, and the pass SHALL read on past it and end in part. Each paired photo SHALL then file with its sidecar's caption.

#### Scenario: A Takeout larger than one call's budget

- **WHEN** a stage holds more sidecars than one `file` call reads
- **THEN** no photo files before every sidecar is read, and every paired photo carries its sidecar's caption

#### Scenario: A sidecar that cannot be read

- **WHEN** a sidecar of the stage cannot be read
- **THEN** the pass reads on, ends in part, and the photos are released

### Requirement: The sidecar pass runs once per stage

The pass over a stage's sidecars SHALL run once, also for a stage that holds JSON files and no sidecar, so that a later call opens none of those files again. A sidecar that reaches the stage after the pass ended SHALL file as an ordinary file.

#### Scenario: An ordinary import with JSON files

- **WHEN** a stage holds JSON files that are no sidecars and is filed in three calls
- **THEN** each JSON file is opened once
- **AND** the sidecars before it and after it keep their pairs, and no JSON file of the stage files as a Source
- **AND** the filing view counts the unreadable file as `IMPORT_SIDECAR_UNREADABLE`, lists it by path as needing review, and lists the partial read with `IMPORT_EXPORT_PARTIAL`

### Requirement: The owner's caption is kept on the photo

A paired sidecar's non-empty `description` SHALL go into the photo Source's description section, through the same redaction as an upload's description. No record of the pass SHALL hold the description unredacted.

#### Scenario: A captioned photo

- **WHEN** a paired sidecar's description is "first day at the lake house"
- **THEN** the photo's Source holds that description, and a search for "lake house" finds the photo

#### Scenario: A caption with a card number

- **WHEN** a paired sidecar's description holds a card number
- **THEN** the photo's Source holds the description with the number removed
- **AND** the stage's sidecar record holds it removed too

### Requirement: A Takeout sidecar's time dates no page

A paired sidecar's `photoTakenTime` SHALL NOT be written to any page's capture record. The pass SHALL NOT read the time: the staged sidecar stays, so that a later change can measure where Google takes it from.

#### Scenario: A received photo

- **WHEN** a JPEG with no EXIF capture time files with a paired sidecar whose `photoTakenTime` is 2019-05-04 10:00:00 UTC
- **THEN** the photo's page records no capture date

### Requirement: A window keeps a likely sidecar with its photo

When the CLI sends a folder in windows, a file whose name reads as a Takeout sidecar's SHALL go in the window of the photo or video that its name fits, as a page's part pages go with their page. The planner SHALL decide this from names and the shipped `source-formats` entry alone and SHALL open no file. A name that fits no file, or a wrong guess, SHALL leave the sidecar in the window that the order gives it, and the sidecar counts as unpaired.

#### Scenario: A Takeout folder that spans two windows

- **WHEN** a folder holds 8 photos with their sidecars and the cell's budget carries about half of it in a window
- **THEN** every sidecar is in the window of its photo, and every photo files with its caption
