## ADDED Requirements

### Requirement: Each shipped artifact declares one licence expression

Every artifact published from this repository SHALL declare one SPDX licence expression covering all the code it ships. The repository licence files, the package metadata, the image labels and the README licence map SHALL agree on that expression for the same artifact.

#### Scenario: The declarations agree

- **WHEN** a release's wheel metadata, image labels, licence files and README licence map are compared
- **THEN** each artifact carries the same licence expression in every place that declares it

### Requirement: The licence follows the release regime

Versions made available after the licence change SHALL license the server, the command-line interface and the images under FSL-1.1-ALv2. The client plugins and the starter skill SHALL stay Apache-2.0. So the wheel and the images, which ship the starter skill, SHALL declare `FSL-1.1-ALv2 AND Apache-2.0`. Versions made available before the change SHALL keep `AGPL-3.0-or-later AND Apache-2.0`.

#### Scenario: A version after the cut

- **WHEN** a version is made available after the licence change
- **THEN** its wheel metadata and image labels declare `FSL-1.1-ALv2 AND Apache-2.0`
- **AND** the README licence map names FSL-1.1-ALv2 for the server and Apache-2.0 for the plugins and the starter skill

#### Scenario: A version before the cut stays as published

- **WHEN** someone inspects a tag or image made available before the licence change
- **THEN** it still carries AGPL-3.0-or-later for the server
- **AND** the repository names the last AGPL-3.0-or-later commit and keeps that licence text available

### Requirement: Each FSL version converts to Apache-2.0 on its second anniversary

The licence of each version made available after the cut SHALL grant Apache-2.0, effective on the second anniversary of the date that version was first made available. The public licence statement SHALL say so in plain words, together with the fact that self-hosting and every non-competing use are free.

#### Scenario: Reading the public licence statement

- **WHEN** a reader opens the README licence section or the hosted service's source offer after the cut
- **THEN** it states that self-hosting is free, that competing hosted use is not permitted, and that each version becomes Apache-2.0 two years after it is first made available
- **AND** it does not call versions after the cut "open source"

### Requirement: The contributor promise matches the licence

The Contributor License Agreement SHALL cover each version that includes a contribution and that the maintainer makes available to the public. For each such version it SHALL promise availability under an OSI-approved licence no later than the second anniversary of the date the version was first made available, and continued availability after that date. The promise SHALL be in force before the first contributor signs. It SHALL NOT promise a licence that a planned version would break.

#### Scenario: A contributor signs after the change

- **WHEN** a contributor signs the agreement
- **THEN** the text they sign carries that promise
- **AND** a version made available under FSL-1.1-ALv2 keeps the promise, because FSL grants Apache-2.0 on the same anniversary

### Requirement: Shipped product code carries no third-party strong copyleft

A version made available after the licence change, and every image published for it, SHALL contain no third-party code under a licence that requires the combined work to carry that licence, such as GPL or AGPL. The requirement covers the code the product imports or links into its own process: its Python distributions, the shared libraries those load, and libraries bundled inside dependency wheels. LGPL libraries MAY ship as separate, replaceable shared libraries.

#### Scenario: Scanning a release image

- **WHEN** the code that the product imports or links in a published release image is listed with its licences
- **THEN** none is GPL or AGPL licensed
- **AND** each LGPL library is a separate shared object

#### Scenario: Contributed code licensed only under the old terms

- **WHEN** the licence change is cut
- **THEN** no line contributed under AGPL-3.0-or-later by a contributor who has not signed the agreement remains in the product
- **AND** no line remains that carries such a contribution's expression

### Requirement: The operating-system layer is recorded, not combined

Separate programs in an image's operating-system layer, such as the shell, core utilities and the package manager, SHALL be treated as aggregated programs, not as code combined with the product. System libraries under a linking exception, such as the GCC runtime libraries, SHALL be treated the same way. The release scan record SHALL list both groups separately from the product's imported code.

#### Scenario: Reading the scan record

- **WHEN** a release scan record is read
- **THEN** it lists the operating-system programs and the linking-exception libraries in their own section, with their licences

### Requirement: Removing a copyleft dependency keeps user-visible behaviour

Replacing a dependency to meet the licence constraint SHALL NOT change what users get. Text extraction, the OCR fallback for scanned pages, document identity evidence, the deployment profile the doctor detects, HEIF decoding and audio and video decoding SHALL keep their behaviour. Their failure reasons SHALL stay distinct outcomes.

#### Scenario: A scanned PDF after the PDF library change

- **WHEN** a PDF whose pages have no text layer is ingested with OCR switched on
- **THEN** each page is rendered and read by OCR as before

#### Scenario: A video codec after the codec layer change

- **WHEN** a video in a format the previous image decoded (H.264, HEVC, VP9 or AV1) is processed
- **THEN** its frames are decoded as before
