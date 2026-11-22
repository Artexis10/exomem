---
exomem_id: 603953d8-ad5f-5ede-8833-c63b54161565
type: procedure
status: active
created: 2026-04-20
updated: 2026-09-30
sources: []
summary: Steps to cut a Pebble Ledger release.
tags: [software, release]
---

# Pebble Ledger release checklist

## Scope

Use for tagging a new version of Pebble Ledger. It does not cover Fernwatch.

## Steps

1. Run the full test suite locally on the main branch.
2. Update the changelog with the user-visible changes since the last tag.
3. Bump the version in the manifest and commit.
4. Tag the commit and push the tag. The release job builds Linux, macOS and Windows binaries.
5. Download each binary and run the import on the sample statements.
6. Publish the draft release once all three binaries behave the same.

## Notes

If the Windows job fails, read [[Notes/Research/pebble-ledger/windows-build-notes]] before re-running it.
