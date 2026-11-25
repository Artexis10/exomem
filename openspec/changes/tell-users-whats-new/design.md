# Design

## Context

Substrate's Home already calls the cell's MCP endpoint through the gateway for levels and activity. The gateway already names the connected app in `x-exomem-app-id`, and the cell already relays a gateway notice as a tool-result field (`service_notice`). Primary source consulted: "Writing effective tools for agents" (pick-by-problem row "The model keeps misusing a tool/MCP surface"): a tool result should carry only the high-signal context the agent needs, in plain language.

## Decisions

### The cell is the one source of release notes

The notes ship inside the image that runs, so Home and the MCP line can never describe a release that the cell does not run. Substrate stores no notes and the release route gains no payload. Rejected: storing notes per release tag in Substrate. It adds a second copy, a contract on the deploy route, and a gap when the deploy collapses several releases into one.

### A note is a file, and its release comes from the tags

Each note is `user-notes/<name>.md` with one paragraph of at most 200 characters, written for a person who does not code. A note belongs to the earliest version tag whose history contains the commit that added the file. A note that no tag contains yet belongs to the version being built. The generator writes `{"schema": 1, "releases": [{"version", "released_on", "notes": [...]}]}`, newest first, keeping the 20 newest releases that have notes. Files stay in the tree after release; the tags decide membership, so no release step edits or deletes them. Renaming or moving a note file makes it a new file, which a later release announces again, so authors never rename one.

The format has one definition, `src/exomem/release_notes_format.py`, which uses the standard library only. The cell imports it to read the file, and the generator loads it by file path, because the release build runs the generator without the package installed. The generator checks the document it writes with that parser before it writes. The generator also applies the note shape rule: a note that breaks it is left out with a `::warning::`, and the build never fails for it, since the note may have skipped the pull-request check.

Closest maintained option: towncrier. It compiles fragments into a file and deletes them in a release commit, which conflicts with release-please owning the release pull request. The gap is the version mapping, about one small script; the maintenance cost is that script and its test.

### The PR check asks for a decision, not a judgement

Whether a user would notice a change is a judgement, so no code decides it. The check only requires that a `feat`, `fix` or `perf` pull request either adds a note or states `User note: none`. A wrong firing costs one line in a PR body. The deciding types are the changelog types that `release-please-config.json` does not hide. The check reads them from that file in the trusted base checkout, so a type added there needs no second edit.

### One line per connected app after an upgrade

The cell keeps, per app id, the last version that app was told about. On a cloud cell with a canonical app id:

- An app with no row gets a row with the running version and no line. A new app hears nothing old.
- An app whose version is older than the running version gets the notes of every release after its version up to the running one. When there are any, the first JSON-object tool result carries `update_notice` = `{version, say}`. `say` begins "Tell the user once in this conversation: " and fits 400 characters. It names the product, gives the newest note, counts the others and points to What's new on Home. The row moves to the running version, also when there are no notes.
- The line is added only after the row write succeeds, so a failing store never repeats it on every call.
- The cell caches each app's version per process: one read per app per process and one write per app per upgrade.
- A call whose app is already at the running version takes no lock. The first call of an app after an upgrade takes a file lock for one small read and write, and a second concurrent call of the same app waits for it, finds the version recorded and carries no line. The lock covers only that write, never the reply.

### A missing notes file is unknown, not empty

The release workflow keeps `continue-on-error` on the generate step, because a notes failure must not block a release. An image then ships without the file. The cell treats a missing or unusable file as unknown: the section returns `releases: []` with `notes_available: false`, and `update_notice` does nothing, so no app's version moves past a release whose notes it never saw. The next image with the file tells every note since the app's version. A Cloud cell logs one cataloged warning when the file is missing or unusable; a development install has no file and logs nothing.

## Risks

- A note written in internal language reaches users. Mitigation: the authoring rule in `AGENTS.md`, the PR template, and the blind review of each PR.
- An image built without history has no notes. Mitigation: the build job fetches tags; a missing file serves `notes_available: false` and never an error, and no app's version moves meanwhile.
