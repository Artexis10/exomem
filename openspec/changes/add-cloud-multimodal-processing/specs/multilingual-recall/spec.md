## ADDED Requirements

### Requirement: OCR reads an image with the installed models for its script

Image and frame OCR SHALL detect the image's script first, and SHALL then read the image with the installed script model for that script together with each installed language pack whose language is written in that script. Each installed pack's script coverage SHALL be read from the pack's own data: the script property of its character set, or the OCR engine's documented equivalent. It MAY instead come from a build-time artifact derived from the installed packs, but SHALL never be a hand-written list in code. A pack SHALL belong to every detected script that its own data covers, so that text in a shared script is read with that language's installed packs; for example, kanji-only Japanese, which detection may report as Han, is read with the installed Japanese packs. The installed script models and language packs SHALL be what the deployment installs: an image build parameter on Cloud, and the local OCR installation elsewhere. When script detection names no script, OCR SHALL read the image with the deployment's configured default language packs. An image whose OCR completed before a pack was installed SHALL keep its completed text unless an explicit reprocessing mode requests it.

#### Scenario: Japanese text in a photo is read

- **WHEN** the deployment installs the Japanese language packs and a photo's detected script is Japanese
- **THEN** OCR reads it with the Japanese script model and the installed Japanese packs
- **AND** the extracted text holds the Japanese characters, which lexical and dense recall then find

#### Scenario: Kanji-only text detected as Han is read with the Japanese packs

- **WHEN** a photo holds only kanji, its detected script is Han, and the deployment installs the Japanese language packs
- **THEN** OCR reads it with the Han script model and the installed Japanese packs

#### Scenario: Unrelated models cost no time

- **WHEN** an image's detected script is Latin
- **THEN** no model for another script reads that image

#### Scenario: Script detection cannot decide

- **WHEN** script detection names no script for an image
- **THEN** OCR reads the image with the deployment's configured default language packs
- **AND** the extraction does not fail because detection was inconclusive

### Requirement: Image search reads queries in every language its text encoder supports

The image lane SHALL encode a query with a text encoder aligned to the space of the stored image vectors. It SHALL accept queries in every language that the encoder's published evaluation covers. Adding a multilingual text encoder aligned to an existing image space SHALL NOT change the stored image vectors.

#### Scenario: A Japanese query finds a photo

- **WHEN** a Japanese query describes what a stored photo shows, and the aligned text encoder covers Japanese
- **THEN** the image lane returns that photo

#### Scenario: A multilingual query encoder keeps the stored vectors

- **WHEN** the query encoder becomes a multilingual encoder aligned to the stored image space
- **THEN** no stored image vector is re-encoded

### Requirement: The image vector sidecar records its vector space and nothing mixes image spaces

The image vector sidecar (`.clip.sqlite`) SHALL record the image model that wrote its vectors, the vector width, the precision and the artifact identity. Every install SHALL encode images with one pinned image model at one precision, so that a personal install and a Cloud cell store interchangeable image vectors. A same-precision substitution that passes the parity bound of the `shared-model-runtime` capability SHALL keep the vector space, and the sidecar SHALL record its new artifact identity. Another model or another precision SHALL be another space. The record names the space, not the writer of each row: rows written before a space-keeping substitution, such as rows from the PyTorch implementation, stay in that space on the strength of the substitution's parity proof. Values calibrated on a space, such as the image-tags threshold, SHALL carry across a space-keeping substitution, and a space change SHALL void them until they are calibrated again. A sidecar with rows and no record SHALL be read as `clip-ViT-B-32` at 512 dimensions and full precision. A query vector SHALL NOT be scored against image vectors of another space. A change of image space SHALL re-encode every stored image vector once, and until an image is re-encoded the image lane SHALL NOT return it.

#### Scenario: A legacy image vector sidecar is read in its own space

- **WHEN** an image vector sidecar has rows and no space record
- **THEN** it is read as `clip-ViT-B-32` at 512 dimensions and full precision
- **AND** only queries encoded for that space are scored against it

#### Scenario: A runtime substitution keeps the space

- **WHEN** a Cloud cell encodes images with an ONNX build of the personal install's image model at the same precision, and the build passes the parity bound
- **THEN** its vectors share the personal install's space, and earlier rows are not re-encoded
- **AND** the sidecar records the ONNX build's artifact identity
- **AND** the image-tags threshold calibrated on that space still applies

#### Scenario: A model change never mixes spaces

- **WHEN** the image model changes and some stored images are not yet re-encoded
- **THEN** a query encoded for the new space is scored only against re-encoded images
- **AND** each remaining image is re-encoded once and then returned by the image lane
