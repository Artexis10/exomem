## Context

Three things in the codebase already sound like "how much should Exomem speak up" and none of them are:

- `mode.py` (`quiet`/`normal`/`performance`) answers "how much of this machine may Exomem use?" It is the only user-visible level in the TUI, which makes the confusion worse.
- `bootstrap(profile=)` (`compact`/`full`/`diagnostics`) trims payload size and changes no behaviour.
- The surface profile registry (`hosted-alpha-agent-v1`) allowlists *which tools exist*, not how eagerly they are used.

The real dial exists but is unnamed: the nudge hooks' environment tunables. This change names it.

## Goals / Non-Goals

**Goals.** One user-facing level; the same level meaning the same thing across every client; the level actually changing behaviour where hooks exist; and a copy-paste artifact for clients where they do not.

**Non-Goals.** Changing what Exomem may touch. The level is about timing and eagerness only — write scope, append-only layers, and governance are unchanged at every level, including `maximal`.

## Decisions

### Share `mode`'s configuration file rather than introducing a per-user one

The obvious instinct is that prominence is per-user while compute mode is per-machine, so prominence should live in `~/.exomem/` on every platform. That is wrong here, for the reason `mode.config_path` already documents: on Windows the MCP server commonly runs as LocalSystem while the CLI runs as the logged-in user, and `~` resolves to two different profiles. Because `bootstrap` serves the active level *from the server*, a home-relative file would let the CLI write a level the server never reads — silently. Sharing `mode`'s path inherits the correct behaviour, plus its existing precedence and atomic-write convention.

Hosted multi-tenancy is unaffected: each cell is an isolated filesystem, so a machine-wide file is already per-tenant there.

Consequence: both writers must merge rather than replace. `write_prominence` mirrors `write_mode` exactly, and a test writes each in both orders to prove neither drops the other.

### Different defaults per surface, rather than one global default

A single default cannot be right for both client families. The hooks exist because prose alone under-fires — their docstrings say so directly. On a hooked client, `balanced` prose plus a per-turn re-arm produces good behaviour. On a hookless client the same prose has nothing restoring it and decays as the thread grows.

So the level is one vocabulary with two shipped defaults: `maximal` where nothing re-arms, `balanced` where something does. Both produce comparable observed behaviour, which is the point — a user should not have to discover that web needs turning up.

The user override always wins over the surface default, and the intended direction of travel is downward: someone who finds it chatty lowers it. Nobody should need to raise it to reach working.

A hook that is executing deliberately ignores surface signals and assumes `balanced`: its own execution is proof that the client has hooks, and trusting a stray `EXOMEM_HOSTED_CELL` in a local shell would double up prose and cadence.

### Duplicate the preset table into each hook, guarded by a test

The hooks are deployed as standalone file copies into `~/.claude/hooks/` and `~/.codex/hooks/`, where the `exomem` package is not importable. They already duplicate small helpers for this reason.

The alternatives were worse: baking the level into installed artifacts goes stale the moment the level changes and would require a reinstall per change; shelling out to `exomem` from a hook adds a subprocess to every prompt submission.

So each hook carries the table and reads the shared config file directly. The drift risk is real and is handled by making it a test failure rather than a convention: the suite renders each hook's table into the canonical shape and asserts equality at every level.

### Keep the contract free of tool names

`_filter_bootstrap_payload` drops any string naming a command the active surface cannot call. A contract phrased as "call `ask_memory` first" would be silently stripped on a reduced surface — exactly the surfaces that most need the guidance. The contract therefore says "search memory", and the tool catalogue elsewhere in the payload supplies the names.

This has one knock-on: the contract is keyed by behavioural axis, and `capture` is also an action name, so the advertised-tool-reference walker in the capability tests reads the key as a tool reference. The walker already has an exemption set for maps whose keys are vocabulary; the contract joins it.

### Hosted artifacts may not name client-local mechanisms

The hosted public-artifact validator forbids `\bhooks?\b`, among other tokens. This is correct — hosted has no hooks, and a reviewer reading about them in a hosted listing would be misled. The hosted skill therefore explains the *consequence* ("nothing outside this conversation will remind you to check") without naming the mechanism.

## Risks / Trade-offs

- **A quiet behaviour change for existing installs.** Mitigated by making `balanced` reproduce today's hard-coded nudge defaults exactly, so an install that never sets a level behaves identically.
- **`maximal` could annoy.** It is deliberately the default only where under-firing is the documented failure, and every level ships a one-paste path down.
- **Generated artifacts move.** Changing the hosted skill moves the skills digest, `compatibility_sha256`, and all three `listing_sha256` values. Re-rendered in the same change; no submission is affected because all three channels remain `draft` with a null publication pointer.
