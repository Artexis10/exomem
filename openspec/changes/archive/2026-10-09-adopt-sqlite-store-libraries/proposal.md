## Why

The SQLite collection store builds ordinary SQL statements and tracks installation migrations by hand. Maintained libraries can own that plumbing before this foundation ships.

## What Changes

- Construct canonical mutation statements, typed tables, and query projections through SQLAlchemy Core.
- Track each store's installation revisions through packaged Alembic migrations.
- Preserve the writer's lease, transaction, accounting, and publication ownership.
- Keep the existing data format, disclosure rules, query bounds, and tool behavior.
- Keep the unreleased capability disabled during this refactor.

## Capabilities

### New Capabilities

None. This change refactors the implementation under the existing collection and query contracts.

### Modified Capabilities

None. The change sets `skip_specs: true` because library adoption preserves the specified storage, migration, and snapshot behavior.

## Impact

The change adds SQLAlchemy and Alembic as runtime dependencies. It affects collection connections, mutation statements, typed storage, query declarations, installation migrations, and snapshot schema validation.

Alembic tracks each user's store independently. Resumable declaration backfills keep their existing application lifecycle.
