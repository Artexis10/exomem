# exomem vs engraph

Point-in-time comparison, inspected 2026-07-01.

- engraph repository: `github.com/devwhodevs/engraph`
- inspected public commit: `f9a95bc96accc792c02ee384d9e6bf768a88c8c8`
- exomem comparison basis: this repository's current command registry, README, sample vault, CI, and release workflow

This is not a permanent ranking. It records the visible architecture and product surface at the inspected commits so future changes can be compared without relying on memory.

## Short version

exomem is ahead on exomem depth: typed pages, source-to-note provenance, supersession/evolution, corpus-aware review queues, multimodal extraction, and one command registry that drives MCP, REST, CLI, and OpenAPI.

engraph is ahead on distribution clarity: public README positioning, prebuilt release archives, Homebrew flow, MIT licensing, and a simpler first-install story.

The practical OSS-readiness target is not to copy engraph's architecture. It is to make exomem's existing substrate easier to verify and install.

## Surface and architecture

| Area | exomem | engraph |
| --- | --- | --- |
| Core model | Typed local KB over markdown: sources, compiled notes, entities, evidence, log, project keys | Markdown vault graph with search/read/write/context tools |
| Surfaces | Single Python command registry drives MCP, REST, CLI, OpenAPI | Rust server exposes MCP tools, REST routes, OpenAPI/plugin metadata |
| Tool count | 25 registry commands plus hand-registered MCP helpers | 25 MCP tools at inspected commit |
| Retrieval | Hybrid keyword/BM25/vector with graph/type/provenance ranking controls | Search/context/vault map over indexed vault content |
| Multimodal | PDF, Office, OCR/images, CLIP visual search, audio/video ASR, diarization hooks | No functional multimodal ingestion found in inspected source |
| Governance | Append-only Sources/Evidence, typed note creation, supersession, audit, reconcile, attention, stale/contradiction review queues | File CRUD, archive/unarchive, health, setup/migration helpers |
| Release posture | Release Please, GitHub release artifacts, PyPI trusted publishing wired off by default | GitHub releases with packaged binaries and Homebrew tap update |
| License | AGPL | MIT |

## Functional comparison

### exomem strengths

exomem has a richer knowledge model than a generic vault CRUD/search server. The core operations distinguish raw sources from compiled notes, preserve evidence, track source ingestion, and use `replace` for explicit supersession rather than silent overwrites. Read-only review tools such as `audit`, `attention`, `evolution`, and provenance reporting surface graph and corpus measurements without asking the server to make semantic judgments.

The multimodal path is also materially broader. exomem can extract and index PDFs, Office documents, OCR text from images, CLIP visual embeddings, and ASR from audio/video. That makes the vault searchable across the real files users already keep, not only markdown text.

The registry architecture is a strong maintenance property: one declaration in `src/exomem/commands.py` drives MCP, REST, CLI, and OpenAPI. The generated `docs/capabilities.md` now documents that surface from the registry, and CI checks it for drift.

### engraph strengths

engraph is easier to understand from the outside. Its README explains the product quickly, the release workflow builds packaged artifacts, and Homebrew/prebuilt binary distribution reduces the burden on users who do not want to install a Python stack.

Its REST and MCP exposure are also broad and practical: search/read/list/map/context plus CRUD, archive/unarchive, migration, identity, and setup routes. That gives users a concrete app-server shape even if the underlying knowledge model is thinner.

### Important caveats

The engraph `find_stale_notes` implementation inspected in `src/health.rs` returned an empty list, so its advertised health surface should not be treated as evidence of deep staleness detection at that commit.

Searches through the inspected engraph source for OCR, CLIP, Whisper, PDF, Office, image, audio, video, and multimodal ingestion did not show functional extraction pipelines comparable to exomem's media stack. That conclusion should be rechecked against newer engraph commits before using it in public claims.

## OSS-readiness implications

The easy wins for exomem are mostly packaging and proof-path work:

- Keep the sample-vault demo deterministic and runnable on a fresh clone.
- Keep `docs/capabilities.md` generated from the command registry so tool-surface claims stay true.
- Keep Release Please and GitHub release artifacts as the baseline release path.
- Enable PyPI trusted publishing when the project is ready to support package-index installs.
- Defer heavier distribution work, such as Docker, Homebrew, or single-file binaries, until the first public install path is stable.

The strategic choice is to explain exomem as a deeper local knowledge substrate, not as a larger list of tools. Tool count is less important than typed provenance, multimodal owned-file retrieval, and measurement-only review workflows.
