# Planning, Records, and user action routing

## Planning and Records

Keep the following layers distinct. Sources are externally received raw material;
Evidence is proof-bearing material; Notes are compiled conclusions; and Entities
are stable reusable identities. Planning is intended future state: goals,
outcomes, initiatives, priorities, horizons, and commitments. Records are
observed state and event history: what happened, when, measurements, sessions,
transactions, and state changes. Review compares Planning intent with Records
reality and feeds explicit decisions back into Planning and Notes. Imported is
adoption staging, not the permanent home of live state.

Planning and Records use separate semantic profiles on one structured-collection
substrate. Planning owns durable intent and prioritisation. Records do not infer
goals, completion, medical conclusions, or personal judgments. A collection may
hold opaque links between a plan and a bounded Records query, but does not resolve
or duplicate the other side. A resolved workflow contract may declare a companion
owner for execution artifacts; without one, Planning works standalone. Companion
references remain opaque, and governance, tool availability, and external
permissions remain separate from the declaration.

Before proactive Planning or Records capture, resolve the current workflow
posture when the workflow route is available, using explicit known-absent scope
values where applicable. Its capture posture is capped by the active prominence
level. Inspect the relevant Planning collection before writing: update a matching
item before creating another. Records are observations, not a transition engine:
only an explicit user change of intent may make a guarded Planning transition;
an outcome under a propose-after-outcome posture may only propose that change.

## Simple front door

Speak to users in simple actions first. Call product commands by default; the
canonical operations are implementation leaves underneath them. Do not ask the
user to choose `Sources`, `Notes`, `Entities`, `Evidence`, graph sidecars, schema
blocks, or supersession internals unless that detail changes what will happen.

Native assistant memory (Claude, ChatGPT, Codex, and similar) is short-term or
behavioural memory for preferences, style, identity facts, working context, and
routing rules such as "use Exomem for my project knowledge." Exomem is long-term
governed memory for sourced conclusions, project context, decisions, failures,
experiments, proof-bearing records, review, and supersession.

| Simple action | User phrasing | Product route |
|---|---|---|
| `ask` | "what do I know," "find what I concluded," "show the context" | `ask_memory(detail="compact", rerank=false)` first; `read_memory` or `ask_memory(deep=true)` when synthesis needs context |
| `remember` | "remember this," "save this conclusion," "write this decision" | `remember`; use `replace_memory` when it supersedes old knowledge |
| `capture` | "save this article/source/transcript," "keep this receipt/record/proof" | Pick the lane first, then the transport. Raw material -> `capture_source` (`content` for text, `files` for attachments). Proof-bearing -> `preserve_evidence` for text, `preserve_artifacts` for attachments, `transfer_artifact` when the client has no file handles |
| `plan` | "save this feature idea," "file this bug for later," "what matters this week" | `plan_memory` for intended future state in a configured Planning collection |
| `record` | "a dated measurement," "a completed session," "a transaction," "the current mileage" | `record_memory` for observed state in a configured Record collection |
| `review` | "review stale knowledge," "what needs attention," "what sources are unprocessed" | `review_memory`; explicit dismiss/snooze/reopen via `triage_memory` |
| `relations` | "review suggested relations," "pay down relation debt," "accept/reject suggested links" | `review_memory(mode="relation-queue")` for the batched read; accept one reviewed candidate via `connect_memory(operation="accept-relation")` (requires the queue fingerprint, target `expected_hash`, and an audit reason); reject via `triage_memory` |
| `connect` | "connect these ideas," "suggest relations," "show the surrounding context" | `connect_memory`; use `operation="context"` for bounded graph, provenance, evidence, and history |
| `adopt` | "what does this existing vault contain," "import/adopt this vault safely" | `adopt_vault(mode="scan-only")` first; explicit modes for manifest/copy/compile planning |
| `maintain` | "check vault health," "fix safe drift," "make Planning/Records files readable" | Remote tools may use `maintain_memory(mode="audit")` or preview one collection with `mode="structured-files"`. Structured-file apply requires its exact preview plan; ordinary `fix`/`reconcile` writes remain host-operator work via `exomem maintain --fix` / `--reconcile` |
| `schema` | "what structure or relation vocabulary recurs," "validate this graph lens" | `schema_memory`; infer before saving, and keep governance optional |

Records routing is semantic: use it for durable observed events or current state
without waiting for a magic verb. When exactly one compatible existing collection
accepts a sufficiently identified observation, the active engagement policy may
append or update it and the agent reports the mutation. Ask one focused question
when collections compete or identity, date, provenance, or ownership is unclear.
When no collection fits, use `record_memory(action="describe")` and propose a
concise collection; the agent must not silently create a long-lived schema.

For a Planning or Records collection stored as Markdown items, YAML frontmatter
is the sole canonical value source and the UUID remains durable identity. A
manifest may declare `item_filename` and `item_presentation` (or the compatible
Records presentation recipe) so the file tree and page read naturally. Prefer
stable descriptive fields such as title for filenames; keep status, priority,
horizon, and other mutable state in frontmatter so ordinary updates do not cause
renames. Treat every managed body block as derived: never edit it as data or read
values back from it. A guarded item update can refresh one block. For an existing
UUID-named collection, call
`maintain_memory(mode="structured-files", collection=...)` to preview every
rename, body change, collision, and inbound-link blocker; apply only with the
returned `plan_id`, `source_snapshot`, and reason. Query a declared Records child
table with `expand_child`; use `expand_children=true` only when the collection
has one unambiguous child container.

Examples:

- "Remember this decision" -> write a concise compiled note and report
  `Saved -> <path>`.
- "What did I conclude about onboarding?" -> `ask_memory` first, cite hits, and
  retry with adjacent terms before treating a miss as meaningful.
- "Save this article" -> `capture_source` with provenance; ask about compiling
  only if a conclusion is present.
- "Here's the transcript/screenshot from that session" -> an attachment is raw
  material, so `capture_source(files=[...])`, classified on both axes. Do not
  reach for an Evidence command because the file handle was convenient: the
  lane is what the artifact is *for*, and a Source can be promoted to Evidence
  later when it turns out to be proof, while the reverse is refused.
- "Keep this receipt for the warranty case" -> `preserve_evidence`, `preserve_artifacts`, or `transfer_artifact`, not as a
  general note.
- "I completed a dated training session" -> resolve exactly one compatible
  collection before `record_memory(action="append")`; keep the session as an
  observed Record, not a compiled conclusion. If none fits, propose a collection
  rather than creating one silently.
- "Show my last three months" -> `record_memory(action="query")` with a bounded date/query shape; use a compiled Note only for an explicit conclusion from that history.
- "The second one is done, the rest can wait" -> one landing, two consequences: `record_memory(action="append")` for the produced deliverable, then `plan_memory(action="triage")` to complete that work item; the others stay queued and nothing else moves. Report it once: "<deliverable> is done and logged; the rest stay queued." Discover the collection with `browse_memory` when none is named, inspect that exact collection with `plan_memory(action="inspect", collection=...)`, then resolve the item with `plan_memory(action="query")` filtered on the title or a natural-key field plus `lifecycle` and `status`.
- "Save this feature idea" -> `plan_memory(action="add")`; use explicit `triage` for a horizon or hierarchy change, never infer it from elapsed time — a stated outcome is evidence, the clock is not.
- "Compile these three sources" -> ensure the three originals are already
  captured as Source/Evidence pages (capture any missing originals first), then
  draft a sourced note with
  `remember(suggestions=true, response_detail="full")` link suggestions, then
  write after the applicable approval rule.
- "Show stale conclusions" -> run the review path and present candidates for
  keep/edit/supersede/archive.
- "This new strategy replaces the old one" -> use supersession so history stays
  visible.
