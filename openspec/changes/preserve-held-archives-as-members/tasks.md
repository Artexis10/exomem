## 1. Held archives as members

- [x] 1.1 Add `archive: members` to `preserve_artifacts` and preserve each held zip through the archive-members writer under the batch commit guard
- [x] 1.2 Fail a handle that is not a held upload and bytes that are not a zip, each naming what to change; refuse `archive` with `adoption` or `transcriptions`
- [x] 1.3 Test that a held export preserved as members imports into a summary collection, and that a refreshed export writes only its new members
- [x] 1.4 Refresh the generated tool schemas, capabilities and cloud plugin, and the tool-surface budget

## 2. Acceptance

- [ ] 2.1 After release, preserve a real export zip over MCP with `archive: members` and import it into its collection
