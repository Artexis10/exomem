# hosted-vault-portability Specification

## Purpose

Define integrity-verifiable export and staged restore for canonical Exomem vault data under stopped maintenance authority.

## Requirements

### Requirement: Portability operations use an explicit quiescence boundary

Source snapshot export SHALL require explicit operator authority, stopped routing, no active mutations, and stopped background writers. Export SHALL flush acknowledged collection writes through the existing replica owner before enumeration. A process that cannot flush a lagging replica SHALL refuse export. Restore SHALL validate into a new unserved staging root and SHALL NOT overlay an active destination. The existing destination mutation owner SHALL govern protected publication and recovery.

#### Scenario: Source reaches export quiescence

- **WHEN** an authorized operator supplies the stopped source maintenance context
- **THEN** export verifies quiescence and flushes the acknowledged collection head before enumeration
- **AND** snapshot construction cannot silently omit acknowledged rows

#### Scenario: Source remains writable

- **WHEN** routing, active mutations, or background writers contradict the stopped maintenance context
- **THEN** export refuses without reporting a complete artifact

#### Scenario: The export process cannot flush a lagging replica

- **WHEN** this process lost its writer lease or custody while the live store remains ahead of its published replica
- **THEN** export reports `COLLECTION_STORE_FLUSH_PENDING`
- **AND** it does not publish an archive that loses acknowledged rows

### Requirement: Export contains canonical owned vault data
A completed export SHALL preserve the exact bytes and vault-relative paths of canonical user-owned Markdown, source artifacts, evidence artifacts, and other durable vault files. It SHALL include governed Markdown such as `Knowledge Base/log.md` and user-visible media sidecars, while excluding runtime service logs, access and query logs, credentials, encryption keys, temporary or lock files, incomplete staging data, and rebuildable machine-local indexes such as embedding, lexical, graph, freshness, and CLIP databases.

#### Scenario: Canonical Markdown and media are exported
- **WHEN** a quiesced vault contains governed notes, sources, evidence, binary media, and user-visible Markdown sidecars
- **THEN** the export contains each canonical file at its original vault-relative path with byte-identical content

#### Scenario: Derived and secret material exists
- **WHEN** the cell also contains rebuildable SQLite indexes, caches, runtime logs, query records, credentials, keys, locks, and temporary files
- **THEN** none of those runtime or derived artifacts is included in the export
- **AND** their exclusion does not remove canonical Markdown or binary content

#### Scenario: Knowledge Base activity history exists
- **WHEN** the canonical vault contains `Knowledge Base/log.md` or governed archive history under the vault
- **THEN** those vault-history files are included even though runtime service and query logs are excluded

### Requirement: Export manifests are integrity-verifiable and host independent
Every completed export SHALL contain a versioned manifest that identifies each exported file by normalized vault-relative path, byte size, and SHA-256 digest. The manifest and archive MUST NOT contain absolute host paths, private cell addresses, runtime credentials, tenant encryption keys, or another tenant's identifiers, and an export result SHALL be published only after all manifest entries have been verified against the completed archive.

#### Scenario: Export completes successfully
- **WHEN** all canonical files are copied into the export artifact
- **THEN** the manifest lists every included file with its relative path, size, and SHA-256 digest
- **AND** verification of the archive against the manifest succeeds before the hook reports completion

#### Scenario: File changes or disappears during snapshot construction
- **WHEN** an enumerated file no longer matches the bytes, size, or digest observed for the quiesced snapshot
- **THEN** export preparation fails without publishing the artifact as complete

#### Scenario: Manifest is inspected outside the source host
- **WHEN** a valid export is opened on a different machine
- **THEN** every content reference is resolvable from vault-relative paths alone
- **AND** the manifest reveals no source-host path, credential, encryption key, or private cell address

### Requirement: Export artifacts remain private until authorized delivery

Export SHALL return an opaque artifact reference and integrity metadata to its authorized caller. It SHALL NOT create a public download URL or expose a service credential. Delivery, expiry, storage policy, and recipient authorization remain outside the archive helper.

#### Scenario: An authorized caller receives a completed export

- **WHEN** snapshot creation and manifest verification succeed
- **THEN** export returns an opaque artifact reference, format, size, and digest metadata
- **AND** it creates no public delivery route

### Requirement: Restore validates into a new staging root before publication
Restore preparation SHALL accept only a supported versioned export, validate its manifest and every file digest, reject absolute paths, traversal, unsafe links, duplicate normalized paths, cross-platform case collisions, unsupported entry types, and configured resource-limit violations, and extract only into a new empty staging root. It MUST NOT overlay or mutate an active vault. The staged vault SHALL become publishable only after structural validation succeeds, and rebuildable sidecars MUST be regenerated rather than restored from the archive.

#### Scenario: Valid export is prepared for restore
- **WHEN** a supported export has a valid manifest, safe paths, matching digests, and content within configured limits
- **THEN** restore preparation writes its canonical files into a new empty staging root
- **AND** the staged paths and bytes match the export manifest exactly

#### Scenario: Restore targets an active or non-empty vault
- **WHEN** restore preparation is asked to overlay an active cell or a non-empty destination
- **THEN** it fails before writing archive content into that destination

#### Scenario: Archive contains an unsafe path or link
- **WHEN** an entry is absolute, traverses above the vault root, is an unsafe symbolic or hard link, duplicates a normalized path, or collides by case with another path
- **THEN** the entire restore is rejected
- **AND** no prepared vault is published

#### Scenario: Manifest digest does not match content
- **WHEN** any restored file differs from its declared size or SHA-256 digest
- **THEN** restore preparation fails and the staging root is not marked publishable

#### Scenario: Export omits rebuildable indexes
- **WHEN** a valid restored vault has no embedding, lexical, graph, freshness, or CLIP sidecars
- **THEN** runtime readiness treats those indexes as absent rebuildable state rather than data loss
- **AND** no derived sidecar from the source host is required for canonical restore success

### Requirement: Staged publication preserves canonical integrity

Publication SHALL preserve the prepared archive's verified canonical paths and bytes. It SHALL use an absent destination and one same-filesystem rename for canonical publication. Protected interrupted publication SHALL retain destination-owned protection and receipt evidence for its exact journal retry. Optional rebuilding SHALL derive from canonical files and SHALL NOT silently change them. Failure to rebuild disposable state SHALL report degraded derived readiness while preserving canonical data.

#### Scenario: A prepared vault is published

- **WHEN** the destination is absent and the prepared paths and bytes match the manifest
- **THEN** publication moves the verified tree into the destination
- **AND** canonical data remains byte-identical

#### Scenario: Derived rebuilding fails

- **WHEN** an optional derived index cannot be rebuilt after publication
- **THEN** publication reports degraded derived state while preserving canonical data

#### Scenario: Derived rebuilding changes canonical bytes

- **WHEN** a rebuild changes a manifest-owned path or byte
- **THEN** publication restores verified canonical bytes or reports a canonical integrity failure
- **AND** it does not report successful integrity verification

### Requirement: Restored canonical data round-trips without semantic rewriting
An export followed by restore preparation SHALL preserve canonical file bytes and relative paths without converting Markdown into database rows, renaming user files, changing frontmatter, or recomputing authored history. Any post-restore index build SHALL derive from the restored files and MUST NOT modify their canonical content as part of readiness.

#### Scenario: Vault is exported and restored unchanged
- **WHEN** a quiesced vault is exported and then prepared for restore into a new cell
- **THEN** every manifest-listed canonical file has the same relative path and SHA-256 digest in the staged vault
- **AND** no authored Markdown or binary file is rewritten by portability or index preparation

### Requirement: Portability requires private maintenance authority

Export and restore preparation SHALL require explicit operator authority. Portability errors SHALL remain content-free and SHALL NOT expose note bodies, extracted text, query text, credentials, encryption keys, public transfer tokens, or absolute source paths.

#### Scenario: An unauthorized caller requests preparation

- **WHEN** a caller supplies no operator authority for export or restore preparation
- **THEN** the helper refuses without changing canonical content
- **AND** the error contains no private content or credentials

### Requirement: Deletion purges media-derived index residue

When a page, media sidecar, or media binary is deleted, the deletion fan-out
SHALL purge every derived index row for that artifact, including CLIP
image/frame vectors and scene-frame derivatives, leaving no sidecar through which
the deleted content remains searchable. Deleting a media binary SHALL trigger the
same fan-out as deleting a Markdown page. A reconcile pass SHALL heal
pre-existing orphaned CLIP rows and `.frames/` directories idempotently.

#### Scenario: Deleting an image purges its CLIP rows

- **WHEN** an image (or its sidecar) is deleted
- **THEN** its CLIP vectors are removed and a subsequent visual search cannot
  return the deleted image

#### Scenario: Deleting a video clears frame derivatives

- **WHEN** a video is deleted
- **THEN** its `<video>.frames/` directory, per-frame sidecars, and per-frame
  CLIP rows are removed

#### Scenario: Deleting a media binary triggers fan-out

- **WHEN** a media binary is deleted directly
- **THEN** the deletion fan-out runs for it (not skipped as non-Markdown) and all
  its derived index rows are purged

#### Scenario: Reconcile heals prior orphans

- **WHEN** a vault contains CLIP rows or `.frames/` directories orphaned by a
  prior deletion and reconcile runs
- **THEN** the orphans are removed, and running reconcile again changes nothing

### Requirement: The vault holds no two siblings with one portable name

No vault folder SHALL hold two children whose names are equal after Unicode NFC normalisation and casefolding, on any host filesystem. The vault, not the host, owns path identity. Path resolution SHALL re-spell a path to the existing sibling with the same key, and an exact spelling match SHALL win. A write that would create a folder whose key matches an existing sibling SHALL be refused with `PATH_VARIANT_EXISTS` naming the existing spelling. A write into a folder that already has a variant sibling SHALL succeed at the requested spelling and carry a warning.

#### Scenario: A capture names a differently-cased scope on a case-sensitive volume

- **WHEN** `Evidence/Food/` exists and a capture names the scope `food`
- **THEN** the artifact lands in `Evidence/Food/`
- **AND** no `Evidence/food/` folder is created

#### Scenario: A folder creation would add a variant

- **WHEN** a write bypasses re-spelling and asks to create a folder whose name matches an existing sibling by NFC and casefold
- **THEN** it is refused with `PATH_VARIANT_EXISTS` naming the existing spelling

#### Scenario: A split already exists

- **WHEN** a write targets a folder that already has a variant sibling
- **THEN** the write lands at the exact spelling requested
- **AND** the result warns about the split

#### Scenario: A project key reuses an existing folder

- **WHEN** a new project key derives a folder name that matches an existing research folder by NFC and casefold
- **THEN** the registration uses the existing spelling
- **AND** an explicit folder that collides with a different existing spelling is refused

### Requirement: A read-only audit lists path variants

`maintain_memory(mode="audit")` SHALL offer the opt-in category `path_variants`. It SHALL report sibling groups equal after NFC and casefold as defects, and groups equal only after NFKC folding and dropping separators as candidates. It SHALL NOT change the vault.

#### Scenario: The audit finds a case group and an NFC group

- **WHEN** a folder holds `Food` and `food`, and another holds a precomposed and a decomposed spelling of one name
- **THEN** the audit reports one defect group for each

### Requirement: Export reports a path collision without naming paths

When a vault cannot be exported because sibling paths collide after NFC and casefold, the export SHALL fail with the code `CASE_COLLISION` and a count of colliding groups. The result SHALL carry no path or name, and its reason SHALL tell the owner to run the `path_variants` audit in their own memory.

#### Scenario: Export of a vault with a split folder

- **WHEN** an export meets two colliding folder spellings
- **THEN** the result code is `CASE_COLLISION` with a group count
- **AND** the result contains no vault path
