## 1. Held archives as members

- [x] 1.1 Add `archive: members` to `preserve_artifacts` and preserve each held zip through the archive-members writer under the batch commit guard
- [x] 1.2 Fail a handle that is not a held upload and bytes that are not a zip, each naming what to change; refuse `archive` with `adoption` or `transcriptions`
- [x] 1.3 Test that a held export preserved as members imports into a summary collection, and that a refreshed export writes only its new members
- [x] 1.4 Refresh the generated tool schemas, capabilities and cloud plugin, and the tool-surface budget

## 2. Acceptance

- [x] 2.1 After release, preserve a real export zip over MCP with `archive: members` and import it into its collection
  Evidence: on release 0.170.0 (2026-11-09), a one-member device export zip went through a held MCP upload (`transfer_artifact` with `hold`, then `exomem attach --handoff-file`) and `preserve_artifacts` with `archive: members`. The result was stored with 1 member and 0 new blobs, because the member's bytes were already stored. An import from its manifest into the device's summary collection completed with the member skipped as already imported and no rows written, as task 1.3 requires of a refreshed export.
