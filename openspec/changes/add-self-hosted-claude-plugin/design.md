## Context

The generator in `src/exomem/cloud_plugins.py` writes the Claude editions from `plugins/cloud/definition.json`. A self-hosted user needs the hooked edition's skills and hooks plus a connection to their own server, in one archive that claude.ai accepts. claude.ai cannot collect the address at install time.

## Goals / Non-Goals

**Goals:**
- A non-technical self-hoster gets the archive with no tooling: open a link, download, upload.
- Every word and file of the archive comes from the generator; the server only fills in its own address.
- Every archive passes the same manifest limits as the generated editions.

**Non-Goals:**
- Changing the existing editions, their plugin names or the hosted plugin.
- Publishing per-server archives anywhere; each server serves only its own.

## Decisions

**The user's own server serves the archive.** The server already knows its public origin from `EXOMEM_BASE_URL`, which OAuth uses as its issuer. A route on that origin needs no input from the user and works from any browser, including a phone. A CLI command would need a shell on the server and the address typed again. `exomem setup --remote` prints the link at the moment the user has just configured the origin.

**The generator owns a server edition as a template.** `claude-server` is a hooked edition with a bundled server whose URL is `https://server.invalid/mcp` (RFC 2606 reserves `.invalid`). Its lead, getting-started text and release notes live in `definition.json` beside the other editions'. The server reads the placeholder from the template's `.mcp.json`, replaces it in the manifest, readme and connection, and sets the plugin name. Every other entry is copied byte for byte. Personalising the connector edition instead would parse its prose and keep a second copy of edition text in code.

**The template ships inside the package and outside the release cut.** The definition marks the edition `packaged`, so the generator writes `claude-server.zip` to `src/exomem/_plugins/`, and `scripts/cloud-plugin.py check` compares it with a fresh build. `src/` is already the only package input of the wheel, the sdist and the container images, so none of them changes. The template keeps the release placeholder in its skills and manifest, and the server fills in its installed release. A release cut therefore leaves the template unchanged, and the release tooling, which regenerates only `plugins/`, needs no new path. The template changes only when skills, hooks or the definition change, in the PR that changes them, which the package check enforces.

**The plugin name derives from the host.** `evermatter-<host>` with non-alphanumerics folded to hyphens. A name over 64 characters keeps an 8-character digest of the host, so two long hosts with a shared prefix stay distinct. The name is stable for one server, so a later upload can replace the earlier plugin. The display name is "Evermatter".

**The route needs no sign-in.** The archive contains the release's public plugin bytes and the server's own public address. A server without `EXOMEM_BASE_URL` registers no route. When the origin cannot produce a valid archive, such as a host too long for the 500-character description, the route answers 503 and the log names the cause; setup and the docs print the link either way.

## Risks / Trade-offs

- Each server's first build re-compresses about 2 MB. The result is cached per origin and installed template, and request-time builds run off the event loop. One self-hosted server receives a handful of requests over its life.
- The archive is about 1.9 MB with no sign-in. Anyone who knows the host can already spend its request quota, so the route adds no new exposure class.
