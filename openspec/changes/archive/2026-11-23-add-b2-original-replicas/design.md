# Direct B2 replica custody

## Context

The media worker replicates encrypted originals through the Store interface. Store declarations specify a failure domain and whether a metadata check reads the stored bytes.

## Decisions

### Reuse the native B2 transport

Add one replica-only adapter and reuse the native B2 client, bounded streaming bridge, queue, and content check. Keep the B2 file ID as the existing opaque version. Bind the credential to one configured bucket ID, name, and prefix on every authorization and refresh. Require read and write access in that scope. Enforce this boundary before opening the copy's source. [Native authorization](https://www.backblaze.com/apidocs/b2-authorize-account) · [Access containment](https://www.anthropic.com/research/how-we-contain-claude)

### Bind accepted bytes to the precise version

Check size and ciphertext SHA-256 before publication. Send SHA-1 for the small upload or each bounded multipart buffer. Retry a transient part failure with the same file ID and part number. If a finish reply is ambiguous, verify the completed file ID. If a small upload reply is lost, a by-name lookup must match the fresh attempt, key, size, and SHA-256 before exact file-info confirms the version. A missing fact leaves the copy operation retryable. [Small upload](https://www.backblaze.com/apidocs/b2-upload-file) · [Multipart upload](https://www.backblaze.com/apidocs/b2-upload-part) · [Multipart finish](https://www.backblaze.com/apidocs/b2-finish-large-file)

Use exact file-info and check bucket, name, version, and upload action. Treat the documented missing-file response as absence. Require the exact returned ID and name before yielding download bytes. A proper subrange requires the exact range and length. The stored SHA-256 describes the upload and supports a presence check. Scheduled content checks retain the full download, ciphertext hash, and authenticated decryption. [Exact file info](https://www.backblaze.com/apidocs/b2-get-file-info) · [Exact download](https://www.backblaze.com/apidocs/b2-download-file-by-id)

### Bound verification and deployment

Reuse a bounded cached authorization for the exact store settings and credential version. Check one provider file-info per known version and retain the existing scheduled deadline. The custody load target is 500,000 versions or 25 TB per store over 30 days. The existing schedule needs about 116 rows per ten-minute round and gives the shared metadata round 120 seconds.

Keep the adapter off until a store is declared. Use a sandbox that retains object versions and an exact bucket-prefix key. Rehearse small and multipart uploads, exact-version reads, and a proper range. Verify the key's scope from its declared capabilities.
