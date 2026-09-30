## Why

CSV, TSV and JSON already have a bounded structured query engine, but preserved uploads receive generic source pages instead of the dataset cards required for discovery and governed dataset reads. Common textual exports also retain their bytes without making their content searchable.

## What Changes

- Create an addressable dataset card atomically with each new CSV, TSV or JSON evidence upload. Bind it to the original bytes and expose bounded structural metadata without copying rows into semantic search.
- Reuse the existing structured query operation for exact rows, filters and aggregates.
- Give new YAML, YML, JSONL, NDJSON, XML and TOML evidence uploads a bounded literal text preview with explicit truncation or encoding status.
- Preserve originals when automatic inspection cannot complete; distinguish byte preservation, discovery and exact query support.
- Keep existing companions and media classification unchanged. This adds no model, service, package dependency or hosted activation.

## Capabilities

### New Capabilities

- `structured-source-preservation`: Atomic, bounded dataset cards and literal text previews for newly preserved evidence uploads.

### Modified Capabilities

- `governance-kernel`: Accept explicitly marked dataset source cards alongside legacy dataset pages; retain unresolved artifact semantics until owner-reviewed classification.

## Impact

The shared preserve leaf used by direct preservation, uploaded bytes and client evidence artifacts gains deterministic inspection of its private staged bytes. Dataset cards follow the existing governance companion format; ordinary recall continues to exclude raw dataset files. Existing file imports and historical companion migration are outside this change.
