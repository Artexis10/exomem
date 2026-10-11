## Why

A personal data export from a device or service is often a zip of thousands of JSON files and tens of millions of samples. Today Exomem cannot take one in:

- **Upload size.** The public host accepts one request of at most 100 MB, the proxy edge's cap. Exomem Cloud accepts one PUT of at most 90 MiB. Nothing resumes an interrupted upload.
- **Duplicate originals.** A later re-export of the same account preserves a new archive of nearly the same bytes, so storage grows by the whole archive every time.
- **Shape.** The importer reads one NDJSON, JSON-array or CSV file. Each JSON value becomes one row; nested arrays cannot. Local times without an offset are refused, and no time zone can be declared.
- **Size.** The store design publishes a full copy of the database into the vault. Tens of millions of imported rows would make each copy several gigabytes.

## What Changes

- **Resumable upload sessions.** `/upload/sessions` speaks the tus 1.0 core protocol with the creation, expiration and termination extensions. The client declares the whole file's SHA-256, and the server verifies it before it preserves anything. A session survives a lost connection and a service restart.
- **Archive members as originals.** A zip preserved with `archive=members` is stored as one compressed, content-addressed blob per distinct member plus a manifest. The manifest records each member's path, data SHA-256 and size, and the archive's SHA-256 and size. A later archive writes only the members that the family does not already hold.
- **Import grammar.** An import can read selected members of an export manifest. A new `json-document` format takes rows from a declared path that can cross nested arrays and read ancestor fields. Time bases can declare an IANA zone, an offset field, seconds from local midnight, or a position in an array. Fold and gap rules for daylight-saving changes are declared, and a numeric field can declare a scale.
- **Derived import collections (contract only).** A collection filled only by imports is derived from its preserved members, its saved mapping and its import log. Its rows stay out of the vault replica and rebuild after a loss. The replica publisher implements this in a later batch.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `client-artifact-preservation`: preserve an archive as content-addressed members.
- `local-client-ingress`: the local listener allows `/upload/sessions`, and resumable sessions work on both listeners.
- `structured-collections`: imports from export manifests and nested JSON documents, declared time bases, and derived import collections.

## Impact

- **New modules:** `archive_members.py` (shared zip checks, member expansion), `upload_sessions.py`, and an `ijson` document reader in the importer.
- **Changed modules:** `preserve.py`, `server_transfer.py`, `service_ingress.py`, `__main__.py` (`exomem attach`), `collection_store/importer.py`, `hosted_transfer_routes.py` (moves its zip checks to `archive_members.py`).
- **Dependencies:** `tzdata` (pinned), so zone rules are the same on every platform and recorded with each import.
- **Not in this change:** hosted upload sessions (they need a new grant kind in Substrate), storage-use admission on Cloud, and the replica publisher's derived-collection behaviour.
