# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0](https://github.com/Artexis10/exomem/compare/v0.2.1...v0.3.0) (2026-07-02)


### ⚠ BREAKING CHANGES

* canonical import name is exomem and canonical env prefix is EXOMEM_*. kb_mcp imports and KB_MCP_* env vars remain supported aliases.
* `get` no longer returns `content` by default; pass include_raw=true for the raw file text. `body`, `frontmatter`, `content_hash`, and `mtime` are unchanged.

### Features

* `exomem setup` — one-command guided local onboarding ([9a679e4](https://github.com/Artexis10/exomem/commit/9a679e4c209fed914006535e7824c079df909499))
* complete the exomem rename — package, env vars, docs, with permanent kb_mcp compatibility ([#81](https://github.com/Artexis10/exomem/issues/81)) ([9f30990](https://github.com/Artexis10/exomem/commit/9f30990e2201f3cdad27002195a73ce0ef6b8ea2))
* find perf overhaul, opt-in usage-aware ranking, get payload dedup ([2e9f753](https://github.com/Artexis10/exomem/commit/2e9f75374f9cbd7e66bfa22b9e73aaa5077114aa))
* find timing diagnostics, compact detail, hot cache, watcher echo suppression ([4d3d51a](https://github.com/Artexis10/exomem/commit/4d3d51af0999c5e6b6be5364802708efffb26dbf))
* get_video_frames — on-demand inline video keyframes over MCP ([1c0294e](https://github.com/Artexis10/exomem/commit/1c0294e6ca9eac58675e5e31910e289f3eb1ffeb))
* make diarization first-class — rediarize backfill, boot readiness line, truthy env gate ([6f6978b](https://github.com/Artexis10/exomem/commit/6f6978bd53e90f489a1c451064276c7ae878c758))
* read-only vault `overview` op — bounded structure report ([34373aa](https://github.com/Artexis10/exomem/commit/34373aaf63ceaef270a49201ae78c73175d9242a))
* video scene detection + persisted, OCR'd scene frames ([#80](https://github.com/Artexis10/exomem/issues/80)) ([4a009db](https://github.com/Artexis10/exomem/commit/4a009dbf19d988dbefe30f330e81f724010845b8))


### Bug Fixes

* re-promote legacy KB_MCP_* env vars after server-side load_dotenv ([#82](https://github.com/Artexis10/exomem/issues/82)) ([473cef7](https://github.com/Artexis10/exomem/commit/473cef799960e9c28cd4d6fb57b413eb8b802caa))

## [0.2.1](https://github.com/Artexis10/exomem/compare/v0.2.0...v0.2.1) (2026-07-01)


### Bug Fixes

* sync package version for release artifacts ([5a3b75b](https://github.com/Artexis10/exomem/commit/5a3b75b0e67c9d244a8fecff6c7960d898a08a89))

## [0.2.0](https://github.com/Artexis10/exomem/compare/v0.1.0...v0.2.0) (2026-07-01)


### Features

* rename project to exomem ([74cb3a0](https://github.com/Artexis10/exomem/commit/74cb3a035a7b009c4b720cc53b3e7c72feda2a5f))

## 0.1.0 (2026-07-01)

### Features

* initial public source release baseline
