## Context

The generator in `src/exomem/cloud_plugins.py` writes three Claude editions from `plugins/cloud/definition.json`. The connector edition (`claude-connector-hooks`) carries every skill and hook and no MCP server. A self-hosted user needs that content plus a connection to their own server, in one archive that claude.ai accepts. claude.ai cannot collect the address at install time.

## Goals / Non-Goals

**Goals:**
- A non-technical self-hoster gets the archive with no tooling: open a link, download, upload.
- The archive comes from release bytes, never from a hand-kept copy.
- Every archive passes the same manifest limits as the generated editions.

**Non-Goals:**
- Changing the generated editions, their plugin names or the hosted plugin.
- Publishing per-server archives anywhere; each server serves only its own.

## Decisions

**The user's own server serves the archive.** The server already knows its public origin from `EXOMEM_BASE_URL`, which OAuth uses as its issuer. A route on that origin needs no input from the user and works from any browser, including a phone. A CLI command would need a shell on the server and the address typed again. `exomem setup --remote` prints the link at the moment the user has just configured the origin.

**The archive personalises the committed connector edition.** The wheel force-includes the generated `claude-connector-hooks.zip`. The server copies every entry byte for byte, replaces the manifest's name, display name and lead, rewrites the readme's getting-started section, and adds `.mcp.json`. A runtime rebuild from `definition.json` would need the definition and assets in the wheel and would duplicate the generator.

**The plugin name derives from the host.** `evermatter-<host>` with non-alphanumerics folded to hyphens, capped at 64 characters. The name is stable for one server, so a later upload replaces the earlier plugin, and two servers do not collide. The display name is "Evermatter".

**The route needs no sign-in.** The archive contains the release's public plugin bytes and the server's own public address, which the requester already used to reach it. A server without `EXOMEM_BASE_URL` registers no route.

## Risks / Trade-offs

- The personaliser relies on the edition's layout: a description with a lead paragraph and a readme with a "Data handling" section. It refuses an archive without them, and the test builds from the real generated edition, so a layout change fails CI.
- Each build re-compresses about 2 MB. The result is cached per origin and installed edition, and the build runs off the event loop. One self-hosted server receives a handful of requests over its life.
