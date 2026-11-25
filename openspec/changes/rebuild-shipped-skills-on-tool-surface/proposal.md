## Why

The shipped skills drifted from the tool surface. The tools moved to typed calls and two doors (`inspect_memory`,
`manage_memory`) that take `<area>.<action>` operations, and their descriptions and `read_instructions(topic=...)` now carry
each call's fields. The skill text kept the older spellings and procedures:

- Agents copy calls that the tools refuse: internal kinds such as `resolve-relation`, top-level search knobs such as
  `rerank`, fields such as `expected_hash` and `requested_relation`, and about twenty retired tool names.
- Workflow procedures skip the tools their job needs. The capture workflow never recalls or queries the Records collection
  that fits before it writes, which is how a fitting collection went unused on 2026-11-24.
- Procedures repeat steps that the server now does itself, such as index and log upkeep and supersession metadata.
- Every workflow skill carries the same 16.5 KB copy of the operating rules and the authoring contract. Its own procedure is
  0.7 to 4.7 KB. The core skill and the server already carry those rules.

The shipped skills are the foundation that the owner's own skills build on, so they must route every task to the current
tools in a few exact steps.

## What Changes

- The core skill holds the invariants and one table from intent to tool or door operation. Per-call fields stay with the
  tool descriptions and `read_instructions(topic=...)`.
- Each workflow skill is a short procedure: when to load it, numbered steps that name the exact calls, and a done check.
  It loads the authoring contract from the reference file shipped beside it, instead of copying it into its body.
- The references keep judgement and policy only. `operations.md` and `operation-routing.md` become one `tools.md`, and
  `frontmatter.md` merges into `page-types.md`.
- A test parses every call in the shipped text and checks it against the published tools and door operations.
- The core skill says how an owner skill composes with a shipped workflow, and how to save a repeated method as an owner skill.
- One server advisory that spelled a retired edit call now spells it from the live surface.

## Impact

- Specs: `skill-loading` and `portable-agent-contract`.
- Code: `workflow_skills.py` (contract validation), `working_set_learning.py` (advisory spelling), the skill scaffold under
  `src/exomem/_scaffold/_Schema/`, the generated plugin trees, and the tests that read the skill text.
- No tool, schema or tool description changes.
