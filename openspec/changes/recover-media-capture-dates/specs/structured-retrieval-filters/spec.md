## ADDED Requirements

### Requirement: The source of a capture date is a filter field

`page.capture_date_source` SHALL be a reserved, indexed scalar page field valued with a `capture-date-sources` key. It SHALL take the equality and membership operators and read as missing for a page with no capture date. For an image or video sidecar with no recorded date it SHALL read as the fallback key beside the derived `page.capture_date`. The in-memory evaluator and the index SHALL derive both from one view of the page.

#### Scenario: Keeping evidence dates only

- **WHEN** a search filters `{"page.capture_date_source": {"$in": ["camera-exif", "matched-original", "file-name"]}}`
- **THEN** a photo whose date is the fallback day is not eligible

#### Scenario: An older index

- **WHEN** a store built before the field exists is opened
- **THEN** it reads as not current, and a filtered search falls back to the in-memory evaluation until the owner pass adds the column
