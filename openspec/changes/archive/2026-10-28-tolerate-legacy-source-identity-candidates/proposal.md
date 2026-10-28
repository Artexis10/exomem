# Proposal

## Why

A readable legacy Markdown file without frontmatter currently blocks every capture that uses durable identity.
Such a file cannot declare a receipt, so refusing unrelated captures makes ordinary legacy content a global availability defect.

## What Changes

- Exclude frontmatter-free legacy pages after canonical companion classification; preserve the reserved root catalog's format check.
- Return a curation warning for each excluded legacy page through existing capture warnings, including reused captures.
- Preserve refusals for unreadable pages, malformed frontmatter, invalid receipts and broken companion bindings.
- Preserve immutable originals without backfilling metadata.

## Capabilities

### Modified Capabilities

- `durable-source-capture`: distinguish legacy pages from receipt candidates and report skipped pages for curation.

## Impact

The existing scan and capture warning path require a small change; no new dependency, service or model is needed.
Capture without durable identity keeps its current behavior.
