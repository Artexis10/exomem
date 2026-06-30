## Why

The Knowledge Base already records how a conclusion changed: supersession links every
replaced note to its successor (`superseded_by`) and predecessor (`supersedes`), the old
page keeps a dated banner with the reason, and `log.md` keeps the `why:` of each edit. But
there is **no way to read that history as a story.** To answer "how did my view on X
change?" today you must find the current note, notice it has a `supersedes` pointer, `get`
the old one, read its banner, follow its `supersedes`, and repeat — many round-trips to
reconstruct a chain that is already fully recorded on disk.

This is direction 4 of the post-moat roadmap (the thinking-evolution view), the last of
the four. It surfaces structure the vault already holds — pure measurement.

## What Changes

- **New `evolution` operation** (new module `src/kb_mcp/evolution.py`) — "how did my view
  on X change over time." It takes a topic **query**, finds the matching notes, resolves
  each into its full **supersession chain**, and returns one ordered **timeline** per
  chain (oldest → newest).
- **Chain resolution from frontmatter pointers.** For each hit it walks backward via
  `supersedes` and forward via `superseded_by` (both written reliably by `replace`),
  collapsing every member of one chain into a single timeline and de-duplicating hits that
  land on the same chain. Chains of length 1 (a note never superseded — no evolution) are
  dropped.
- **Each timeline version carries its own structurally-extracted claims** (reusing
  `context_pack._extract_claims`: lede + headline-section lines + `##` outline), its date,
  and — on every non-head version — the **recorded transition reason** (the supersession
  banner / the `why:` logged at the supersession edit). The active head carries no
  transition.
- **A `supersedes` reader on `ParsedPage`** (alongside the existing `superseded_by`), so
  the backward walk is a clean frontmatter read.
- **Registry entry** exposes `evolution` on MCP + REST (`/api/evolution`) + CLI
  (`kb evolution`) from one `_SPEC` line, via the same leaf — no per-surface code.
- The MCP schema-fidelity fixture is regenerated to include the new tool.

It stays **pure-substrate**: every field is text the user wrote (each version's claims, the
recorded transition reasons) or pointer/date arithmetic over the supersession graph the
vault already records. **No server-side LLM, no generated "here's how your thinking
changed" narrative** — the server orders the versions and surfaces each one's claims; the
brain (Claude/Max) reads consecutive versions and infers the evolution. Read-only: it
mutates nothing and does not touch `find` ordering. Same status as `find` / `attention` /
the context pack.

Out of scope (future, deliberately): in-place edit-history timelines (the finer-grained
`log.md` `why:` stream within a single note — noisier, deferred); any LLM summary of the
change; cross-chain synthesis.

## Capabilities

### Added Capabilities
- `thinking-evolution`: an `evolution(query)` tool that returns, per supersession chain
  matching the topic, an ordered timeline of every version with its structural claims,
  date, and recorded transition reason — measurement-only, bounded with explicit
  truncation, reachable on every surface.

## Impact

- Code: new `src/kb_mcp/evolution.py` (`build_timelines` pure builder + `evolution()`
  entry + dataclasses). New `ParsedPage.supersedes` property in `find.py`. One
  `op_evolution` leaf + one `_SPEC` entry in `commands.py`. The MCP schema-fidelity fixture
  (`tests/fixtures/mcp_tool_schemas.json`) is regenerated to add the new tool.
- Behaviour: purely additive — a new read-only tool. No existing tool, `find` ordering, or
  the vault changes. Default-on (read-only and cheap: one `find` + short pointer walks;
  claim extraction has no model dependency).
- Tests: new `tests/test_evolution.py` (torch-free, tmp-vault chains): ordered timeline +
  transitions, single-note chain excluded, multi-chain, dedup, truncation, determinism;
  plus registry/surface assertions and an end-to-end `op_evolution` test.
- Deploy: a new MCP tool requires reconnecting the claude.ai connector to appear; REST/CLI
  need only a restart. No data migration.
