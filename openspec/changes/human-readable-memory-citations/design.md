## Context

Governed pages carry an immutable `exomem_id`, and Exomem resolves the corresponding
`exomem://memory/<uuid>` reference through a rebuildable index. That identity is the
right machine contract because it survives note moves and renames. Search and read
results already keep identity and presentation separate by returning a human-readable
`title`, a current `path`, and a canonical `ref`.

The gap is in agent guidance. The bootstrap contract and installed skill explain when
canonical refs are useful, but they do not clearly prohibit showing the opaque ref in
normal conversation. Some clients therefore render a UUID where the user needs the note
name. This is a presentation leak, not an identity or resolver defect.

The relevant guidance surfaces are the generic MCP bootstrap in
`src/exomem/commands.py` and the installed scaffold in
`src/exomem/_scaffold/_Schema/`. The change must keep those surfaces semantically
aligned without introducing blocking hooks, server-side prose rendering, or reasoning
models.

## Goals / Non-Goals

**Goals:**

- Preserve immutable note identity and move-safe canonical references.
- Make note titles the default citation shown in user-facing agent prose.
- Give agents a deterministic fallback and disambiguation rule based on the existing
  path field.
- Keep generic bootstrap clients and installed-skill clients on the same contract.
- Cover the contract with lightweight, model-free regression tests.

**Non-Goals:**

- Removing, replacing, or making users edit `exomem_id` frontmatter.
- Changing the canonical reference grammar or reference index.
- Adding slugs to canonical references.
- Adding a citation formatter or a new citation field to search/read responses.
- Blocking tool execution when an agent violates presentation guidance.
- Hiding refs from machine-readable tool results or explicit diagnostic requests.

## Decisions

### Keep identity opaque and presentation human-readable

`exomem_id` and `exomem://memory/<uuid>` remain the canonical machine identity. In
normal user-facing prose, agents cite the hit's title and omit the raw canonical ref.
When a title is ambiguous, the agent adds the current vault-relative path or another
short human-readable disambiguator. When no usable title is available, the path or file
name is the fallback; the UUID is not.

This preserves the property the UUID was introduced for while making the default output
legible. Removing UUIDs or using mutable title slugs would weaken rename safety. A
hybrid slug-plus-UUID URI would still expose an implementation-shaped identifier and
would add grammar and migration complexity without improving normal prose.

### Make this an agent contract, not a response-shaping API

The server continues returning `title`, `path`, and `ref` as separate fields. Guidance
will tell agents to use `ref` for tool arguments, durable machine state, and explicit
debugging, while using `title`/`path` for conversation. Agents must not create Markdown
links whose visible source includes the raw custom-scheme target; plain title-first
citations are the portable default across clients.

Adding a `citation_label` or rendered Markdown field was rejected because it duplicates
data already present on every relevant hit, spends response tokens, and still cannot
control how a client displays prose. No server-side model or formatter is involved, so
the pure-substrate boundary is unchanged and there is no optional/heavy capability to
gate or soft-fail.

### Keep both guidance surfaces equivalent

The bootstrap workflow guidance, scaffold `SKILL.md`, and detailed operations reference
will express the same rules:

- show the title by default;
- add the path only for clarity or disambiguation;
- keep the canonical ref for machine use;
- reveal the raw ref when the user explicitly asks for it or when presenting diagnostic
  or automation output where the identifier itself is the subject.

The bootstrap contract version will be bumped because its guidance changes, without
adding a new response field. Tests will assert the semantic rules in the bootstrap and
the shipped scaffold so future edits cannot silently restore title/UUID ambiguity.

## Risks / Trade-offs

- **Prompt guidance cannot force every third-party client to comply** -> put the rule in
  both authoritative agent surfaces, make it direct rather than advisory, and pin it
  with contract tests. A blocking hook is intentionally out of scope.
- **Duplicate titles can make title-only citations ambiguous** -> permit a short
  vault-relative path as a disambiguator instead of exposing the UUID.
- **A malformed legacy hit may lack a useful title** -> fall back to its path or file
  name and retain the raw ref only in machine state.
- **Hiding refs by default can slow debugging** -> explicitly allow the ref when the
  user asks for the canonical identifier or when the identifier itself is under
  inspection.
- **Bootstrap and scaffold wording can drift** -> add focused tests for both surfaces,
  checking behavior rather than requiring byte-identical prose.

## Migration Plan

Ship the bootstrap and scaffold guidance together. Generic clients receive the updated
rule on their next bootstrap call; installed skills receive it through the normal
Exomem skill install/update path. Existing notes, IDs, refs, and indexes require no
migration. Rollback is a documentation/contract revert and does not touch vault data.

## Open Questions

None. The existing `title`, `path`, and `ref` fields provide all required information.
