# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.1](https://github.com/Artexis10/exomem/compare/v0.4.0...v0.4.1) (2026-07-02)


### Bug Fixes

* diarization soft-fail boundary guard + thread vault_root to named attribution ([c5bc82c](https://github.com/Artexis10/exomem/commit/c5bc82c3667b317799c5a66fe93a68a88ab8f0a7))

## [0.4.0](https://github.com/Artexis10/exomem/compare/v0.3.0...v0.4.0) (2026-07-02)


### Features

* Docker distribution — lean/ml images, compose with tunnel profiles, gated GHCR publish ([#90](https://github.com/Artexis10/exomem/issues/90)) ([88286b2](https://github.com/Artexis10/exomem/commit/88286b2d91c80914def999db894aa7d56bfdd5cb))
* lexical-first instant start — non-blocking boot, background warm, readiness defer gates ([#86](https://github.com/Artexis10/exomem/issues/86)) ([3e42418](https://github.com/Artexis10/exomem/commit/3e424183c539de851684bb7dd373963e35e7ed89))
* packaged `exomem demo` + wheel-path onboarding gate — prove value in 30 seconds ([#87](https://github.com/Artexis10/exomem/issues/87)) ([8056308](https://github.com/Artexis10/exomem/commit/8056308bcf7e533f98a19c5cbb3a76c7135edffa))
* remote connector quickstart — doctor --probe, ngrok no-domain path, ingress docs rework ([#89](https://github.com/Artexis10/exomem/issues/89)) ([33084b0](https://github.com/Artexis10/exomem/commit/33084b07f354458be0d9e472a786494053c76417))
* semantic video segments — timed transcripts, fused topic segmentation, transcript_match_at ([#88](https://github.com/Artexis10/exomem/issues/88)) ([5561ec1](https://github.com/Artexis10/exomem/commit/5561ec159929e4301ca57bb09b6b3913d344232c))


### Bug Fixes

* **cli:** first-run polish — entry points target exomem, warm names the missing extra ([#91](https://github.com/Artexis10/exomem/issues/91)) ([c7971f1](https://github.com/Artexis10/exomem/commit/c7971f1d421eb993281bdba2a05fa70b1fb1db8f))

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
