## ADDED Requirements

### Requirement: Agents see stored images as image content

`read_media` with `kind: "image"` SHALL return each requested stored image as MCP image content, preceded by one text block that leads with a short outcome and lists, for each image, its stored path, original file name, capture time when EXIF records one, original pixel size and shown pixel size. A call SHALL accept one `path` or up to six `paths`, and SHALL return the images in the requested order. Each image SHALL be turned upright by its EXIF orientation and downscaled so its longest side is at most `max_side` pixels (default 1568, clamped to 256..2048). The server SHALL read each original through the same custody verification and full-disclosure release decision as an original download, and SHALL refuse a withheld, missing or changed original without saying which. The stored original SHALL NOT change.

#### Scenario: An agent looks at a preserved photo

- **WHEN** an agent calls `read_media` with `kind: "image"` and the path of a stored JPEG shot in portrait
- **THEN** the result holds one upright JPEG image block whose longest side is 1568 pixels
- **AND** the text block names the file and its EXIF capture time
- **AND** the stored original's bytes are unchanged

#### Scenario: A format the server cannot decode

- **WHEN** an agent asks to see a stored camera raw file
- **THEN** the refusal is `IMAGE_FORMAT_UNSUPPORTED` and names `transfer_artifact` download as the route to the original

### Requirement: Short download links for originals

When the process that mints an original's HTTPS download also serves `/download`, the link SHALL carry a random id of at least 128 bits instead of the signed grant, and `/download` SHALL resolve that id to its grant and decide the download exactly as it decides the grant. An unknown or expired id SHALL answer 404 with `DOWNLOAD_LINK_EXPIRED` and a reason that tells the person to ask for a fresh link. A full signed grant SHALL keep working. `transfer_artifact` SHALL name the HTTPS delivery as preferred whenever it offers one.

#### Scenario: A person downloads a photo from a short link

- **WHEN** an agent prepares a download of a stored photo and the person opens the returned link
- **THEN** the link is shorter than 80 characters
- **AND** the response is the exact original bytes

#### Scenario: A link outlives its grant

- **WHEN** a person opens a link whose id the server does not hold
- **THEN** the answer is 404 `DOWNLOAD_LINK_EXPIRED` telling them to ask for a fresh link
