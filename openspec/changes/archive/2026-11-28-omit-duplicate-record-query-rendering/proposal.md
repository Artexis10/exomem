# Proposal

## Why

The default `query_records` reply repeats its rows as a formatted JSON string. This consumes context in clients that only need the structured rows.

## What Changes

- Return the structured rows, their source and pagination data by default.
- Keep formatted strings for queries that already support an explicit `output_format`. Keep the existing direct Records command output.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `records` defines the default reply of `query_records` and how to request a formatted result.

## Impact

The MCP tool's default output loses the `rendered` field where it currently includes one. Callers that use that field can pass `options.output_format`.
