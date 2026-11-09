## Context

The roster rows used: "The model keeps misusing a tool/MCP surface", "Tool definitions/results consume huge context" and "Tool arguments are malformed or required fields are absent". All three point to fewer, typed, well-named tools, definitions loaded on demand, and arguments that the server can derive.

A grouped command (`remember`, `edit_memory`, `record_memory` and so on) is the identity that carries receipts, the writer lease, egress and the ledger. A public tool name that middleware rewrites to its grouped command before authorization is a rendering (`PublicCommandNameMiddleware`). This change keeps that seam and changes which public names exist.

## Decisions

**D1. One public table, with the existing operation entries kept as the routing record.** `public_surface` declares the 21 typed tools and the two doors.
- A typed tool is a pydantic wire model, a translation to one grouped command and its leaf fields, and a reverse spelling for hints. The wire model supplies both the published schema and strict validation.
- A door operation is `area.action`. It maps to one operation entry (command, selector, literal constraints, kind). The entries still carry the accepted fields, required fields and effect, so `assert_coverage` and the type checks stay the single source.
- The old nested renderings stay as internal routing records. No client can name them. This replaces the plan to add flat and door forms to `Rendering`: the same outcome with less new machinery.

**D2. Selection.** A typed tool with a selector (`edit_note.action`) maps the selector value to one operation. A door maps `operation` to one entry, and the entry's constraints must accept the arguments. A startup check proves that every entry has exactly one public home (a typed tool, a door operation, or the dropped list), that an inspect_memory operation is a read, and that a manage_memory operation is a write.

**D3. Door wire.**
- `operation` is a closed enum, about 3.7 KB. `arguments` may arrive as a JSON string, which the server decodes once.
- A read twin shares its name with the write it previews: `files.create` on inspect_memory validates, and `files.create` on manage_memory writes.
- manage_memory carries the write and destructive annotations and no `preview`, so a preview never prompts a write confirmation. inspect_memory carries only the read annotation.
- Door operations keep their leaf field names. They are rare, the owner runs them, and `read_instructions` documents them on demand.

**D4. Help.** `read_instructions(topic)` is the one help channel. It generates from the operation entries and `operation_schema._variant_branch`, so the help, the skill routing reference and the validation cannot drift. A tool name returns its arguments and an example. A door name lists its operations. An operation name returns its schema from either door, including the credential rule text of governance operations. There is no `help` operation: on manage_memory it would carry a destructive annotation, and a client would ask the user to confirm a read.

**D5. Wrong calls.** An unknown tool name, an unknown door operation, a wrong-door operation and a malformed argument each return the existing agent refusal with a `retry` that `capabilities.active_spell_call` renders, plus up to five `candidates`. An internal command name is refused as an unknown tool before authorization runs.

**D6. Guards the server derives.** A short id is `<7 base32 of the identity>.<4 base32 of the version>`, as the page id of the earlier change. The leaf compares the version after the write is admitted and under the writer lease. The translation layer never reads the vault, because a read there would run before admission and before the visibility filter.

| Guard today | Agent sends | Server derives |
|---|---|---|
| Page `expected_hash` | page id as `note` | The earlier change's leaf compare. |
| `unit_ref`, `expected_fingerprint`, `target_fingerprints` | `observation` (unit id) | The unit id resolves in the observation leaf after the visibility filter. A stale id refuses with the current id. |
| Records and plan `item_key`, `expected_item_version` | `record` or `plan_item` (row id) | The row id resolves in the records leaf; a stale id refuses with the current id. |
| `expected_container_hash`, `expected_manifest_hash` | nothing | The leaf fills the container hash under the writer lease. |
| `vocabulary_ref`, `vocabulary_fingerprint` on accept | `relation` (optional) | The one decided relation-type item for the relation. When several match, the refusal lists them as candidates. |
| `draft_id`, `draft_hash`, `draft_token` | `draft` (the id from the preview reply) | Draft lookup. |
| Relation review hash and reason | `unlinked_reason` | Server-side binding of the reviewed-none decision to the committed content. |
| `validate_only`, `response_detail` | `preview`, `detail` | One rename at the seam. |

**Deliberate relaxation.** The parent page hash and the container hash become fill-at-commit under the writer lease. The reason: the unit id, row id or page id already carries the version that the agent saw, so a second hash guards nothing the id does not. The write still refuses when the id's version is stale, and the lease serializes the commit. REST and CLI keep the explicit fields. A write that names no id (an observation added to a page, a record appended by natural key) never carried a version guard.

**D7. Old names.** `Command.callable_names` and `PreferredToolDiscovery` stop exposing internal names, and `PublicCommandNameMiddleware` refuses any name that is not listed. The legacy aliases (`EXOMEM_MCP_LEGACY_COMPAT`) leave the server.

**D8. Cloud.** `cloud_artifact_transport` maps the lane to the called tool (`save_source` or `save_evidence`) and compares that with the grant's `op`. The gateway signs the called name. The two Cloud transfer tools stay listed on Cloud, because the gateway intercepts them by name today.

**D9. Hooks.** A stamp from `write_selectors()` as (tool, discriminator, kind) replaces the capture hook's word regex over tool names. A call with `preview: true` does not count as landed.

## Risks

- **Directory review.** A directory reviewer once held back grouped tools for "operations that are not individually exposed". The doors bring that shape back. Rescan before the directory submission task.
- **Cloud file handles.** If a cell ships before the gateway intercepts `save_source` and `save_evidence`, attached-file saves fail on Cloud. This does not gate local releases.
- **One digest move per client.** A later change to upload_files forces a second ChatGPT refresh.
- **Door retry rate.** A rate above about 20% would promote the busiest door operations to typed tools.
- **Claude Code deferral.** Whether Claude Code loads about 25 KB up front without ToolSearch is unverified.
