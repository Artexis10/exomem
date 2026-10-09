## ADDED Requirements

### Requirement: OCR reads an image with the installed models for its script

Image and frame OCR SHALL detect the image's script first, and SHALL then read the image with the installed script model for that script together with each installed language pack written in that script. The installed script models and language packs SHALL be what the deployment installs: an image build parameter on Cloud, and the local OCR installation elsewhere. They SHALL NOT be a list in code. The mapping from a language pack to its script SHALL be the closed mapping that the OCR engine publishes. When script detection names no script, OCR SHALL read the image with the deployment's configured default language packs. An image whose OCR completed before a pack was installed SHALL keep its completed text unless an explicit reprocessing mode requests it.

#### Scenario: Japanese text in a photo is read

- **WHEN** the deployment installs the Japanese language packs and a photo's detected script is Japanese
- **THEN** OCR reads it with the Japanese script model and the installed Japanese packs
- **AND** the extracted text holds the Japanese characters, which lexical and dense recall then find

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

The image vector sidecar (`.clip.sqlite`) SHALL record the image model that wrote its vectors, the vector width, and, for a served artifact, the same artifact identity that a recall sidecar records. Every install SHALL encode images with one pinned image model, so that a personal install and a Cloud cell store interchangeable image vectors. A sidecar with rows and no record SHALL be read as `clip-ViT-B-32` at 512 dimensions. A query vector SHALL NOT be scored against image vectors of another space. A change of image model SHALL re-encode every stored image vector once, and until an image is re-encoded the image lane SHALL NOT return it.

#### Scenario: A legacy image vector sidecar is read in its own space

- **WHEN** an image vector sidecar has rows and no space record
- **THEN** it is read as `clip-ViT-B-32` at 512 dimensions
- **AND** only queries encoded for that space are scored against it

#### Scenario: A model change never mixes spaces

- **WHEN** the image model changes and some stored images are not yet re-encoded
- **THEN** a query encoded for the new space is scored only against re-encoded images
- **AND** each remaining image is re-encoded once and then returned by the image lane
