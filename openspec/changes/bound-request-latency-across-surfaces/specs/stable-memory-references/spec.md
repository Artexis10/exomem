## MODIFIED Requirements

### Requirement: Disclosure-neutral reference resolution

Resolution of memory references for a governed read SHALL return, for each requested reference, every canonical page that holds it, duplicates included, before the read applies any authorization. Resolution SHALL choose its source from vault-wide state and the calling command alone: the in-process identity projection when the server holds one, otherwise one canonical scan of the Knowledge Base for the whole batch. The choice SHALL NOT depend on which references were asked for, which of them the vault holds, or who may read the pages that hold them. Resolution MAY read an identity projection that the server holds, and SHALL create, rebuild, or refresh no index, cache, or sidecar.

A read-only command MAY answer from a projection that is behind only an outside edit that the watcher has not yet published. It SHALL then re-check, against the page on disk, that each resolved candidate the caller may see still holds the requested identity, and SHALL take the strict lookup for a candidate whose identity changed. A command that writes SHALL take the scan while the projection is not current. A reference that the projection does not hold SHALL NOT cause a scan.

Both sources SHALL apply one page membership rule, so the source never changes the answer. A page belongs when it is Markdown by a case-insensitive `.md` suffix, lies under the Knowledge Base outside the directories that ordinary scans exclude, and carries a well-formed `exomem_id`. Line endings SHALL NOT change whether a page's `exomem_id` is read. A missing or malformed `exomem_id` maps to no page. Pages in a directory that the identity census never admits but ordinary scans read, such as `trash/`, SHALL be held in the projection in a table used only for reference resolution and kept out of identity uniqueness checks, so that those pages resolve as the scan resolves them.

A reference that the vault does not hold and a reference that only pages hidden from the reader hold SHALL produce identical responses. This requirement bounds the resolution work only. When the read that follows resolution refuses a restricted caller, its completion time SHALL follow the `get-payload-shape` requirement "Restricted direct-read refusals complete at a fixed floor".

#### Scenario: A hidden twin does not shadow a visible page
- **WHEN** one page that the reader may see and one page that the reader may not see both hold the same reference, and the server holds a current identity projection
- **THEN** resolution returns both pages, authorization removes the hidden page, and the read returns the visible page exactly as it does through the scan
- **AND** a reference that two pages visible to the reader both hold reads as ambiguous through either source

#### Scenario: An absent reference and a hidden-only reference read the same
- **WHEN** one vault holds a reference only in a page that the reader may not see, and an otherwise identical vault does not hold that reference
- **THEN** a read of the reference returns the same not-found response in both vaults
- **AND** both resolutions use the same source and perform the same scan count

#### Scenario: A projection that is not current falls back to the scan
- **WHEN** the server holds no identity projection, or a command that writes resolves a reference while an edit made outside the server is pending
- **THEN** resolution performs one canonical scan for the whole batch and answers from that scan
- **AND** resolution creates or refreshes no projection, index, cache, or sidecar

#### Scenario: A read answers from a projection behind an outside edit
- **WHEN** an edit made outside the server is pending and a read-only command resolves a reference
- **THEN** resolution answers from the projection without a scan
- **AND** a resolved candidate whose page no longer holds the requested identity is resolved through the strict lookup instead
- **AND** a reference that the projection does not hold reads as not found without a scan

#### Scenario: A vault with a trash directory resolves through the scan
- **WHEN** the Knowledge Base root holds a `trash/` directory and the server holds a current identity projection
- **THEN** resolution answers from the projection's reference-only table without a scan
- **AND** a reference that a page in `trash/` holds resolves to that page, as it does through the scan
