# Spec Delta

## ADDED Requirements

### Requirement: A withheld field reads as missing in every filter

When a field is withheld from the caller for a page, every structured filter SHALL evaluate that field as missing for that page. This includes reserved fields, generic frontmatter-pointer filters and `$exists`. The withholding SHALL be decided per page before the filter is evaluated. A filter on a withheld field SHALL NOT change the result count, the ranking or the error that the caller sees, compared with the same page lacking the field.

#### Scenario: A pointer filter on a withheld position

- **WHEN** a caller who may not read a photo's original filters on that photo's position through a frontmatter pointer with `$exists: true`
- **THEN** the photo does not match
- **AND** the same filter with `$exists: false` matches it
