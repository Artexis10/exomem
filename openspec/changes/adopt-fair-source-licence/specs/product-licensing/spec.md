## ADDED Requirements

### Requirement: Each shipped artifact declares one licence

Every artifact published from this repository SHALL declare exactly one licence for its own code. The repository licence file, the package metadata, the image labels and the README licence map SHALL name the same licence for the same artifact.

#### Scenario: The declarations agree

- **WHEN** a release's wheel metadata, image labels, `LICENSE` and README licence map are compared
- **THEN** each artifact's licence is the same in every place that declares it

### Requirement: The licence follows the release regime

Releases cut after the licence change SHALL carry FSL-1.1-ALv2 for the server, the command-line interface and the images. Releases cut before it SHALL keep AGPL-3.0-or-later. The client plugins and the starter skill SHALL carry Apache-2.0 under both regimes.

#### Scenario: A release after the cut

- **WHEN** a release is cut after the licence change
- **THEN** its wheel metadata, its image labels and the repository `LICENSE` name FSL-1.1-ALv2
- **AND** the README licence map names FSL-1.1-ALv2 for the server and Apache-2.0 for the plugins and the starter skill

#### Scenario: A release before the cut stays as published

- **WHEN** someone inspects a tag or image published before the licence change
- **THEN** it still carries AGPL-3.0-or-later
- **AND** the repository names the last AGPL-3.0-or-later commit and keeps that licence text available

### Requirement: Each FSL release converts to Apache-2.0 on its second anniversary

The licence of each release after the cut SHALL grant Apache-2.0 effective on the second anniversary of that release's publication. The public licence statement SHALL say so in plain words, together with the fact that self-hosting and every non-competing use are free.

#### Scenario: Reading the public licence statement

- **WHEN** a reader opens the README licence section or the hosted service's source offer after the cut
- **THEN** it states that self-hosting is free, that competing hosted use of a release is not permitted, and that each release becomes Apache-2.0 two years after it is published
- **AND** it does not call releases after the cut "open source"

### Requirement: The contributor promise matches the licence

The Contributor License Agreement SHALL promise that every released version which includes a contribution becomes available under an OSI-approved licence no later than the second anniversary of its release. The promise SHALL be in force before the first contributor signs. It SHALL NOT promise a licence that a planned release would break.

#### Scenario: A contributor signs after the change

- **WHEN** a contributor signs the agreement
- **THEN** the text they sign promises OSI availability no later than the second anniversary of each release that includes their contribution
- **AND** a release under FSL-1.1-ALv2 keeps that promise

### Requirement: Shipped artifacts carry no third-party strong-copyleft code

A release cut after the licence change, and every image published for it, SHALL contain no third-party code under a licence that requires the combined work to carry that same licence, such as GPL or AGPL. This covers source files, Python dependencies and the shared libraries bundled inside dependency wheels. LGPL libraries MAY ship as separate, replaceable shared libraries.

#### Scenario: Scanning a release image

- **WHEN** the shared libraries and Python distributions in a published release image are listed with their licences
- **THEN** none is GPL or AGPL licensed
- **AND** each LGPL library is a separate shared object

#### Scenario: Contributed code licensed only under the old terms

- **WHEN** the licence change is cut
- **THEN** no line contributed under AGPL-3.0-or-later by a contributor who has not signed the agreement remains in the product

### Requirement: Removing a copyleft dependency keeps user-visible behaviour

Replacing a dependency to meet the licence constraint SHALL NOT change what users get. Text extraction, the OCR fallback for scanned pages, document identity evidence, HEIF decoding and audio and video decoding SHALL keep their behaviour. Their failure reasons, including memory exhaustion, SHALL stay distinct outcomes.

#### Scenario: A scanned PDF after the PDF library change

- **WHEN** a PDF whose pages have no text layer is ingested with OCR switched on
- **THEN** each page is rendered and read by OCR as before
- **AND** a page that exhausts the memory cap ends as a memory stop, not as a missing page
