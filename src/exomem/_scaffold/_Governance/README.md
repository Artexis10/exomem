# _Governance

This folder is where you author reviewed disclosure-policy source for your
Knowledge Base — separate from the compiled, structured material it governs.
It is never indexed as content: it never shows up in search results, and
nothing you write here is treated as a note, a source, or evidence.

Policy is strict YAML, one document per file, under three subfolders:

- `scopes/*.yaml` — which pages a policy applies to. A scope is a named
  selector: a path glob, a project, a tag, a type, an explicit reference, or
  some combination, plus an optional `exclude` for carving out exceptions.
- `rules/*.yaml` — for a scope, an audience, and a disclosure ceiling. A rule
  can optionally be conditioned on a declared purpose.
- `grants/*.yaml` — standing exceptions that can raise (never lower) a
  ceiling for a scope and audience.

Every document carries `governance_version: 1` and an immutable `id`
(a ULID). Unknown fields on a recognized document are treated as a compile
error — the last known-good policy stays in effect until it's fixed. An
unrecognized file under this folder is ignored with a warning, not an error.

Use `govern_memory` to inspect, propose, review, commit, suspend, resume, or undo
policy. On an enrolled vault these YAML files are pending source until a reviewed
commit activates one immutable policy/projector/catalog tuple; a direct edit does
not silently replace the active tuple.

This tree and Exomem's internal governance state, including
`Knowledge Base/.governance.sqlite` and its transactional siblings, are reserved.
Generic file, dataset, media, transfer, and download commands intentionally hide
or refuse them. A `RESERVED_PATH` response means to use `govern_memory`, not to
retry through an alias, link, alternate spelling, or direct file operation.

Before first enrollment, an absent folder or empty document set means governance
is not configured. After enrollment, deleting this folder or its internal state
does not disable governance. Exomem fails closed until the owner repairs or
migrates the authority state.
