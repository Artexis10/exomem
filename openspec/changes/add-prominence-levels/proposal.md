## Why

Exomem has no user-tunable control over how much it participates in a conversation. The only dial today is a set of undocumented environment variables on the capture and retrieve nudge hooks (`EXOMEM_*_NUDGE_MIN_CHARS`, `..._COOLDOWN_SEC`, `..._DISABLE`), which no user is expected to find. `exomem mode` (`quiet`/`normal`/`performance`) and `bootstrap(profile=)` both sound like the missing knob and are not: the first governs machine footprint, the second payload size.

This matters most on clients that cannot run hooks. The hooks exist precisely because skill prose is passive — their own docstrings record that "over a long thread the model forgets to check, so 'auto-save' quietly never fires." claude.ai, ChatGPT, and the hosted service have no hook to re-arm that check, so the same instruction text decays with nothing to restore it. Those are exactly the surfaces non-technical users adopt, and where the product looks like it "doesn't remember."

The shipped hosted skill projection compounds this: it is nine lines carrying one sentence of engagement guidance, against 1010 lines locally.

## What Changes

- Add four canonical prominence levels — `off`, `light`, `balanced`, `maximal` — each a contract over three behavioural axes: recall, capture, narration.
- Resolve the active level as `EXOMEM_PROMINENCE` env → the shared config file → a surface-dependent default, mirroring `mode`'s precedence exactly and persisting to the same file so neither setting can clobber the other.
- Default hookless surfaces (hosted, claude.ai, ChatGPT) to `maximal` and hook-capable surfaces (Claude Code, Codex) to `balanced`, so equivalent real-world behaviour costs the user nothing on either.
- Make the level change behaviour and not only prose: each level maps to concrete nudge-hook tunables, with explicit environment variables still winning.
- Report the active level and its full contract from `bootstrap()` under `engagement`, so a generic MCP client can follow it without the skill loaded.
- Add `exomem prominence [level]` to show, set, and print the implied hook tunables.
- Parameterise the skill's `## Proactive engagement` section by level, and correct its claim that the skill never runs on hooks — the repository ships exactly those hooks for two clients.
- Expand the hosted skill projection to carry the maximal contract and honour a user-supplied lower level.
- Publish copy-paste custom-instruction blocks per level for claude.ai and ChatGPT, each inside ChatGPT's per-field character budget.

## Capabilities

### New Capabilities

- `prominence-levels`: The user-facing engagement level, its resolution and persistence, its projection into the agent contract and the nudge hooks, and the per-client defaults.

### Modified Capabilities

None. `mode` is untouched beyond sharing its config file, and no tool schema, governance rule, or write path changes.

## Impact

- Adds `src/exomem/prominence.py`, an `engagement` block in the `bootstrap` payload, and an `exomem prominence` subcommand.
- Changes the default nudge cadence only where a level is set; an install that never sets one keeps today's behaviour, because `balanced` reproduces the current hard-coded defaults exactly.
- Changes the hosted skill text, so the hosted skills digest, `compatibility_sha256`, and all three directory `listing_sha256` values move. The generated packages and directory packets are re-rendered in the same change. No submission is affected: all three channels are still `draft` with a null publication pointer.
- Duplicates the preset table into the two nudge hooks, which are deployed as standalone copies and cannot import the package. A drift test asserts the copies stay identical to the canonical table.
- The level governs *when* Exomem acts and never what it may touch. Governed write scope, append-only `Sources/` and `Evidence/`, and access policy are unchanged at every level.
