# Design — Unified command surface (single source of truth)

## Context

Three surfaces, one set of leaf functions, but the *surface wiring* is hand-maintained per surface
(server.py MCP decorators + REST routes + `post_tools` OpenAPI list; `__main__.py` CLI). The leaf
functions (`find_module.find`, `note_module.note`, …) take `vault_root` + kwargs and return plain
dicts/dataclasses — so a single declaration per operation can generate *every* surface.

## Goals / non-goals

- **Goal:** one registry as the genuine single source of truth generating MCP + REST + CLI +
  OpenAPI; a real `kb` CLI (reads + writes) with Endstate ergonomics; provably no change to what
  Claude sees.
- **Non-goal:** auth changes (CLI local-only; REST keeps `KB_MCP_REST_API_KEY`), streaming,
  SKILL/scaffold doc merges.

## The registry (`commands.py`)

```python
@dataclass(frozen=True)
class Param:
    name: str
    type: str            # "str" | "int" | "bool" | "list[str]" | "dict"
    required: bool = False
    help: str = ""
    cli_positional: bool = False

@dataclass(frozen=True)
class Command:
    name: str            # canonical op name — identical across all surfaces
    leaf: Callable       # leaf(vault_root, **kwargs)
    description: str      # the FULL tool description Claude reads (was the MCP docstring)
    params: tuple[Param, ...]
    surfaces: frozenset  # subset of {"mcp", "rest", "cli"}
    tier: int = 1
    cli_writes: bool = False  # marks a vault-mutating op (for CLI grouping/confirms)
```

`COMMANDS: tuple[Command, ...]` enumerates every operation, carrying its description + param specs.

## How one registry drives all four surfaces

- **MCP (the key piece): generated via `bind_vault`.** `bind_vault(leaf, vault_root)` returns a
  callable whose `__signature__` is the leaf's signature **minus `vault_root`**, whose `__doc__` is
  the registry `description`, and which calls `leaf(vault_root, **kwargs)`. FastMCP introspects that
  bound callable exactly as it does a hand-written wrapper, so `mcp.tool(bind_vault(cmd.leaf, root))`
  registers a tool with the same input-schema + description. The build loops `COMMANDS` with `"mcp"`.
  - **Fidelity guard (non-negotiable):** a snapshot test captures every current tool's
    `inputSchema` + `description` (committed as a fixture), and asserts the registry-generated tools
    are **byte-identical**. This is what makes a 24-tool migration safe — Claude's view can't drift.
  - **Exceptions:** any tool whose generated schema can't match cleanly (the wide `note`, the
    `note`-docstring project-key injection, anything with a non-introspectable signature) stays
    hand-registered and is listed in `HAND_REGISTERED_EXCEPTIONS`; the snapshot test skips those but
    asserts the exception list is explicit (no silent gaps).
- **REST:** loop `COMMANDS` with `"rest"` → `@mcp.custom_route("/api/<name>", POST)` with one generic
  handler (gate → JSON body → coerce to leaf kwargs via `Param` specs → `run_in_threadpool` →
  envelope). Existing 9 names + leaf calls preserved (pinned by a back-compat test).
- **OpenAPI:** generated from the `Param` specs (real per-parameter schemas + the description);
  deletes the `post_tools` hand-list.
- **CLI:** loop `COMMANDS` with `"cli"` → an `argparse` subparser per op (positional for
  `cli_positional` params, `--flags` for the rest; `list[str]` repeatable/comma; `bool` store_true).
  Exposes **reads and writes**. `note`'s type-specific args use a `--field key=value` escape so the
  CLI stays clean. Endstate ergonomics: global `--json` (envelope) vs human default; structured
  `Error [CODE]: message` + remediation; exit 0/1/2. Local-only (already has vault access).

## Decisions

- **Console scripts:** add `[project.scripts]` `kb = "kb_mcp.__main__:main"` and
  `exomem = "kb_mcp.__main__:main"` — `kb find "…"` is the daily command; `exomem` the namespaced
  alias. `python -m kb_mcp` keeps working.
- **Shared envelope** (`cli_ops.envelope`): `{success, data, error: {code, message, remediation}}`,
  mirroring Endstate. REST 200 → `{success:true, data:…}`; REST 4xx → `{success:false, error:{…}}`.
  Personal facade → adopting the envelope is a low-risk contract improvement (the one documented
  behavior change).
- **Description lives in the registry, not the wrapper.** Moving each tool's description into
  `Command.description` is the migration's bulk; the snapshot test guarantees each moved string is
  reproduced verbatim.
- **Tier-2 honored:** `tier=2` ops respect `KB_MCP_DISABLE_TIER2` for MCP/REST/CLI exposure.
- **Binary-blob guard preserved** in the REST coercion for text fields (`BINARY_BLOB_REJECTED`).

## Risks

- **FastMCP schema fidelity** is the central risk — mitigated by the byte-identical snapshot test +
  the explicit exception list. Build order: snapshot FIRST, then migrate, so every step is checked.
- The REST refactor must be behavior-preserving for the existing 9 (before/after test).
- `note`'s width: handled by keeping it a hand-registered MCP exception + a `--field` CLI escape.
