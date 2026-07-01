## ADDED Requirements

### Requirement: Per-Request Freshness Snapshot

The system SHALL compute markdown freshness for a single `find` request at most once per scope: at
most one KB markdown stat-walk and at most one vault markdown stat-walk per request, shared by every
consumer that needs that scope's freshness within the same request (the BM25 index and the wikilink
resolver). A `scope="kb-only"` request that triggers no vault-scope work MUST NOT perform a
vault-wide stat-walk.

#### Scenario: One KB walk and one vault walk per request

- **WHEN** `find` is called with `scope="kb"` and a non-empty query that also triggers auto-widen's
  vault-scope check
- **THEN** the KB markdown tree is stat-walked at most once for that request
- **AND** the vault markdown tree is stat-walked at most once for that request, shared between
  auto-widen and any other vault-scope freshness check
- **AND** the returned hits are identical to the same request today

#### Scenario: kb-only scope never walks the vault

- **WHEN** `find` is called with `scope="kb-only"`
- **THEN** no vault-wide markdown stat-walk occurs for that request

### Requirement: Corpus Freshness Keys Detect Deletes, Renames, And Backdated Replacements

The BM25 index cache and the wikilink resolver cache SHALL use a freshness key that changes whenever
the set of markdown files in their scope changes by deletion, rename, or replacement with a file at
an older mtime than the file it replaced, in addition to changing on file-count or max-mtime
increases. A rebuild MUST be triggered whenever this key changes.

#### Scenario: Deleting a file invalidates the BM25 index

- **WHEN** a markdown file indexed by a previously built BM25 index is deleted and no remaining
  file's mtime increases
- **THEN** the next matching `find` request rebuilds the BM25 index for that scope

#### Scenario: A rename invalidates the wikilink resolver

- **WHEN** a markdown file is renamed without changing the vault's file count or any file's mtime
- **THEN** the next `find` request that needs the wikilink resolver rebuilds it rather than reusing
  the resolver built before the rename

#### Scenario: A backdated replacement invalidates the BM25 index

- **WHEN** a markdown file is replaced by a new file at the same path with an older mtime than the
  file it replaced, such that the scope's max mtime does not increase
- **THEN** the next matching `find` request rebuilds the BM25 index for that scope

### Requirement: Per-Page Derived-Text Reuse

The system SHALL compute each page's normalized body text, normalized title text, and stemmed token
set at most once per page revision, and SHALL reuse the computed values for every `find` call made
against that revision. A page revision change (the markdown file's mtime changing) MUST invalidate
the previously computed derived text for that page, and the next access MUST reflect the new
content.

#### Scenario: Repeated queries against an unchanged page reuse derived text

- **WHEN** two different `find` queries are evaluated against the same unchanged page
- **THEN** the page's normalized body, normalized title, and stemmed token set are computed once and
  reused for both queries
- **AND** both queries observe the same derived text and the same match/no-match outcome they would
  have observed if it had been recomputed for each query

#### Scenario: Editing a page invalidates its derived text

- **WHEN** a page's content is edited and its mtime changes
- **THEN** the next `find` call against that page computes fresh normalized body, normalized title,
  and stemmed token set from the new content

### Requirement: Startup Cache Warm-Up

The system SHALL warm the BM25 index (KB and vault scope), the wikilink resolver, and the parsed-page
cache during server startup, unless disabled by `KB_MCP_DISABLE_WARMUP`, so that a subsequent `find`
call does not pay first-call index/resolver/page-parse construction cost. Warm-up SHALL soft-fail
per stage without preventing server startup and MUST NOT change `find`'s returned results.

#### Scenario: Warm-up primes caches before the first query

- **WHEN** the server starts with warm-up enabled
- **THEN** the BM25 index for KB scope, the BM25 index for vault scope, the wikilink resolver, and
  the parsed-page cache are populated before the first `find` call is served
- **AND** the first `find` call's results are identical to what it would return without warm-up

#### Scenario: Warm-up can be disabled

- **WHEN** the server starts with `KB_MCP_DISABLE_WARMUP` set
- **THEN** no warm-up work is performed at startup
- **AND** `find` still returns correct results, built lazily on first use as it does today

#### Scenario: A warm-up stage failure does not block startup

- **WHEN** one warm-up stage (for example, building the BM25 vault-scope index) fails
- **THEN** the server still starts successfully
- **AND** the failure is logged without raising, and other warm-up stages still run
