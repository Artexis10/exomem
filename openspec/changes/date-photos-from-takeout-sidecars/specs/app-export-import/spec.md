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

A recognised Takeout sidecar SHALL pair with an image or video in its own staged folder only when its name and its `title` both fit that file under the entry's naming facts, no other file fits it, and that file fits no other sidecar. A pair SHALL be recorded without any position.

#### Scenario: Duplicates with the copy number in either place

- **WHEN** a folder holds `IMG.jpg`, `IMG(1).jpg`, `IMG(2).jpg`, `IMG.jpg.json`, `IMG.jpg(1).json` and `IMG(2).jpg.supplemental-metadata.json`, every sidecar titled `IMG.jpg`
- **THEN** each sidecar pairs with its own photo, and no photo receives another's time or caption

#### Scenario: A long name cut by Takeout

- **WHEN** a photo's name and its sidecar's name are both cut at the lengths the entry names, and the sidecar's `title` holds the full name
- **THEN** the sidecar pairs with that photo

#### Scenario: Two sidecars fit one photo

- **WHEN** two sidecars in one folder both fit the same photo
- **THEN** neither pairs, and both are counted unpaired

### Requirement: A Takeout sidecar is counted by its outcome, never filed

A recognised sidecar SHALL never file as a Source. When filing reaches done, the filing view SHALL count each sidecar in `left_out` under exactly one reason, from its photo's filing result: `IMPORT_SIDECAR_APPLIED` (its photo filed and received a time or caption), `IMPORT_SIDECAR_EMPTY` (its photo filed and it carried nothing), `IMPORT_SIDECAR_PHOTO_NOT_FILED` (its photo was skipped or failed), or `IMPORT_SIDECAR_UNPAIRED` (listed by path). It SHALL NOT add to the stage total as a derived file.

#### Scenario: A Takeout album

- **WHEN** a staged folder holds 50 photos and their 50 sidecars, 48 of which carry a time or caption
- **THEN** 50 photo Sources file and no JSON Source does, and the filing view counts 48 `IMPORT_SIDECAR_APPLIED` and 2 `IMPORT_SIDECAR_EMPTY`, and reads done

#### Scenario: A sidecar without its photo

- **WHEN** a sidecar's photo is in another archive part that was not staged
- **THEN** the sidecar files nothing, and the filing view counts and lists it as `IMPORT_SIDECAR_UNPAIRED`

#### Scenario: The same Takeout imported again

- **WHEN** the same Takeout is imported again unchanged
- **THEN** every photo is skipped as already imported, no JSON Source files, and every sidecar counts as `IMPORT_SIDECAR_PHOTO_NOT_FILED`

### Requirement: Photos wait for their sidecars

The images and videos of a stage that holds recognised sidecars SHALL stay out of selection until every sidecar in the stage is read and the pairs are decided. Each paired photo SHALL then file with its sidecar's time and caption.

#### Scenario: A Takeout larger than one call's budget

- **WHEN** a stage holds more sidecars than one `file` call reads
- **THEN** no photo files before every sidecar is read, and every paired photo carries its sidecar's time and caption

### Requirement: The owner's caption is kept on the photo

A paired sidecar's non-empty `description` SHALL go into the photo Source's description section, through the same redaction as an upload's description.

#### Scenario: A captioned photo

- **WHEN** a paired sidecar's description is "first day at the lake house"
- **THEN** the photo's Source holds that description, and a search for "lake house" finds the photo
