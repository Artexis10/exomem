# Source fields (compatibility)

Releases up to 0.204.0 read the required source-page fields from this file when they start, so a vault
that one of them opens after a rollback still starts. The fields and their meaning live in
[page types](page-types.md#source). Delete this file once no supported rollback target predates 0.205.0.

### source

| Field | Required |
|---|---|
| `source_type` | yes |
| `captured` | yes |
| `ingested_into` | yes |
