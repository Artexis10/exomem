# _Governance

This folder is where you author disclosure policy for your Knowledge Base —
separate from the compiled, structured material it governs. It is never
indexed as content: it never shows up in search results, and nothing you
write here is treated as a note, a source, or evidence.

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

If this folder is absent, or has no policy documents in it yet, there is no
governance policy in effect — nothing changes about how your vault behaves.
