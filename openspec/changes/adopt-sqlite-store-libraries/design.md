## Context

See `proposal.md` for motivation. The existing `add-collection-query-engine` change owns the collection, disclosure, migration, and snapshot contracts.

Each vault has one external canonical collection database. The writer lease serializes mutations, and SQLite backup produces portable snapshots.

The current database compatibility version is 8. Historical installation migrations preserve immutable history and source identity.

## Goals / Non-Goals

The change replaces ordinary SQL construction and installation revision sequencing with maintained libraries. It preserves canonical values, history, authorization, publication, and supported upgrade paths.

An ORM, generic backend framework, analytical engine replacement, and new query features are outside this design. Resumable index backfills remain application operations.

## Decisions

### One writer connection owns transactions

`WriterConnection` owns one normal SQLAlchemy `Connection` through an engine with `NullPool`. Its documented `driver_connection` remains available for SQLite-specific operations.

The DBAPI creator retains the existing connection factory. The connection keeps WAL, FULL synchronization, foreign keys, recursive triggers, and explicit transaction control.

The documented SQLAlchemy begin event issues `BEGIN IMMEDIATE`. SQLAlchemy owns physical connection closure, commit, and ordinary rollback.

After a failed commit, cleanup clears SQLAlchemy transaction state and rolls back the DBAPI connection if SQLite still reports an active transaction. Deferred foreign-key failures require this fallback.

The release cache finishes only after physical transaction cleanup. Existing lease, thread, and publication ownership stay in their current modules.

### Raw reads stay outside Core mutation scopes

Migration preflight reads and foreign-key pragma changes use the raw connection before the outer transaction starts. Foreign-key disabling inside a transaction cannot support historical table rebuilds.

Existing raw read snapshots stay wholly raw. Core execution occurs only inside an explicitly owned SQLAlchemy mutation scope, preventing a second implicit begin.

Schema initialization returns with both connection transaction states idle. Public SQLite exceptions retain their existing type and message through `DBAPIError.orig`.

### Core constructs statements and declarations

Current canonical table references live in `collection_store/tables.py`. Dynamic declarations derive from `Layout` and `ProjectionPlan`, which remain their authorities.

Dynamic Core metadata is immutable and scoped to the complete declaration shape and physical generation. A collection name or installation version alone cannot identify a cached statement.

Core replaces mutation statement construction in `_write_item`, `_insert_txn`, `_advance`, and `_pending`. The existing `_execute` boundary accounts raw `total_changes`, including trigger effects.

Core also constructs typed table inserts, upserts, and projection declarations. Repeated row writes reuse statements rather than compile one statement per row.

Encoding, decoding, payload hashes, version identity, and history triggers retain their current meaning. Core types must not coerce exact stored scalar representations.

Dedicated query readers keep their raw TEMP workflow. `ProjectionPlan.create_ddl(temporary=True)` emits compatible SQLite DDL through Core compilation, including TEMP index placement.

### Alembic sequences installation revisions

Packaged `migrations/env.py` and eight revision files use Alembic's supported online connection-sharing interface. `Config.attributes["connection"]` supplies the writer-owned SQLAlchemy connection.

Revision identifiers `1` through `8` describe the historical chain. The initial revisions call the frozen historical transformations through the raw connection.

Alembic replaces `MIGRATIONS` as execution authority. Future installation revisions use Alembic operations; runtime declaration backfills retain their checkpoint and publication contracts.

Each store has the standard `alembic_version(version_num)` table. Core creates its standard-shaped TEXT primary key as STRICT before Alembic stamps or upgrades it.

Alembic tracks installation schema only. Manifest versions, type versions, mapping generations, and backfill checkpoints retain their existing runtime authorities.

User declarations do not advance the installation revision or supply migration scripts. Alembic autogeneration excludes runtime-owned tables.

The compatibility version remains 8 because this adoption changes no canonical data layout. `store_meta.schema_version` becomes a derived compatibility field.

When Alembic metadata exists, readers, writers, and snapshot validation require one supported revision that agrees with the compatibility field. A truly absent table remains valid legacy metadata.

An empty, malformed, unknown, or conflicting existing table is not legacy absence. The shared validation check prevents migration selection or restore from contradictory schema claims.

This refusal protects stored data. A wrong refusal blocks the affected store's access and costs its owner repair time; it must not fence out valid legacy version-8 stores.

### One atomic installation transaction

The writer reads the legacy version and enforces the existing ceiling before migration. It disables foreign keys outside the transaction when historical table rebuilding requires it.

One outer SQLAlchemy transaction covers initial revision stamping, explicit-target upgrade, trigger repair, compatibility metadata, and the final foreign-key check. Revisions do not commit individually.

Failure rolls back data and both revision markers together. Cleanup restores foreign-key enforcement and preserves same-handle reuse, including deferred commit failure.

Snapshots without Alembic metadata remain readable. A new writer stamps them under the existing lease when opened.

### Runtime evolution keeps its existing limits

Guarded collection revision validates existing rows synchronously. Indexed field changes keep their current compatibility checks; arbitrary populated type changes remain behind their existing migration gates.

An unfinished index build can be replaced or reversed through a guarded revision while the old ready mapping remains usable. Committed checkpoints survive restart.

Changed rollups rebuild their derived buckets and use admitted base queries meanwhile. The rebuild does not promise the old rollup's performance.

Restoring a completed declaration requires current data to satisfy the restored definition. Typed encoding conversion remains forward-only, and import cancellation retains committed rows.

Library adoption adds no runtime downgrade script or arbitrary reversible schema edit. User-authored types and general collection migration retain their existing P4/P5 and P1b/P2/P3 gates.

## Risks / Trade-offs

- SQLAlchemy adds imports and dependencies. The scoped installed workflow measures their practical cost without claiming unmeasured speed gains.
- Type conversion could alter stored values. Existing exact value, history, and payload checks must pass through the new statement boundary.
- Mixed transaction ownership could leave partial writes. Raw/Core scope separation and failed-commit reuse provide the decisive proof.
- Revision bookkeeping adds files. Per-store inspection and maintained sequencing replace the custom dispatcher; historical transforms remain explicit.
- Old binaries could reject or discard new metadata. A real version-8 read/write/snapshot/restore round trip must prove compatibility before delivery.

## Proof Plan

The highest-level check uses the installed package to create, import, resume, correct, query, snapshot, restore, and write again in isolated invented state.

The check compares canonical values, history, source identities, disclosures, receipts, and restored writes. Existing files retain their file authority throughout.

Focused checks cover historical upgrades, version ceilings, metadata corruption, STRICT dynamic tables, append-only triggers, release accounting, and raw TEMP projections.

The compatibility check runs a new writer, an actual old version-8 reader/writer, an old snapshot/restore, and the new writer again. It verifies preserved metadata and subsequent writes.

Both existing failed-commit reuse cases remain mandatory. The v4-to-v8 upgrade checks foreign-key handling during referenced-table rebuilding.

One populated collection journey revises indexes and rollups, writes between batches, interrupts and reopens the writer, completes activation, and reverses an unfinished build.

That journey preserves canonical history, declaration guards, admission, mapping authority, and checkpoints. It does not claim unsupported runtime schema edits.

One clean full corpus, native Windows checks, installed outcome proof, and exact independent review close the integrated delivery batch.

## Migration Plan

1. Integrate the latest main successor into the isolated task branch, preserving both source contracts.
2. Add the maintained dependencies with the pinned uv writer, preserving package installation across supported runtimes.
3. Replace SQL construction and installation sequencing, preserving the compatibility version and canonical rows.
4. Verify the installed workflow and historical compatibility, yielding preserved data and valid subsequent writes.
5. Merge the reviewed, green batch and coordinate the ordinary release, yielding verified publication without overlapping aliases.

The root delivery owner coordinates publication with the existing release owner. The capability stays disabled until its separate activation evidence is complete.

A compatible version-8 binary is the library rollback target after the round-trip proof passes. An incompatible pre-store downgrade retains its existing export or backup requirement.
