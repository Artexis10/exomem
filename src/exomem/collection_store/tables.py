"""Core references for the installed schema; historical DDL stays in migrations."""

from sqlalchemy import Column, Integer, MetaData, Table, Text, bindparam
from sqlalchemy.dialects.sqlite import insert

metadata = MetaData()
# These names are the closed installation schema, never user-defined fields.
items = Table("items", metadata,
    Column("row_id", Integer, primary_key=True), Column("collection_id", Text),
    Column("item_key", Text), Column("natural_key", Text), Column("row_version", Integer),
    Column("schema_version", Integer), Column("values_json", Text), Column("body", Text),
    Column("payload_hash", Text), Column("view_path", Text), Column("created_txn", Integer),
    Column("updated_txn", Integer), Column("governance_json", Text), Column("encoding", Text))
collections = Table("collections", metadata,
    Column("collection_id", Text, primary_key=True), Column("generation", Integer),
    Column("audit_head", Text), Column("updated_txn", Integer))
txns = Table("txns", metadata,
    Column("txn_id", Integer, primary_key=True), Column("transition_id", Text),
    Column("collection_id", Text), Column("operation", Text), Column("profile_operation", Text),
    Column("generation_before", Integer), Column("generation_after", Integer),
    Column("manifest_version_before", Integer), Column("manifest_version_after", Integer),
    Column("actor", Text), Column("why", Text), Column("request_id", Text),
    Column("request_hash", Text), Column("receipt_json", Text), Column("committed_at", Text),
    Column("prev_event_hash", Text), Column("event_hash", Text), Column("commit_seq", Integer),
    Column("store_head_hash", Text), Column("legacy_event_json", Text))
item_versions = Table("item_versions", metadata,
    Column("row_id", Integer), Column("row_version", Integer), Column("values_json", Text),
    Column("body", Text), Column("payload_hash", Text), Column("txn_id", Integer))
item_sources = Table("item_sources", metadata,
    Column("row_id", Integer), Column("row_version", Integer), Column("ordinal", Integer),
    Column("source_ref", Text))
audit_effects = Table("audit_effects", metadata,
    Column("txn_id", Integer), Column("ordinal", Integer), Column("row_id", Integer),
    Column("item_key", Text), Column("effect", Text), Column("effect_label", Text),
    Column("version_before", Integer), Column("version_after", Integer),
    Column("hash_before", Text), Column("hash_after", Text), Column("source_ref", Text))
version_identity = Table("version_identity", metadata,
    Column("row_id", Integer, primary_key=True), Column("row_version", Integer, primary_key=True),
    Column("encoding", Text), Column("payload_hash", Text), Column("txn_id", Integer),
    Column("schema_version", Integer))
projection_state = Table("projection_state", metadata,
    Column("path", Text, primary_key=True), Column("collection_id", Text), Column("row_id", Integer),
    Column("kind", Text), Column("pending_row_version", Integer), Column("pending_sha256", Text),
    Column("state", Text), Column("install_json", Text))
store_meta = Table("store_meta", metadata,
    Column("key", Text, primary_key=True), Column("value", Text))
alembic_version = Table("alembic_version", metadata,
    Column("version_num", Text, primary_key=True), sqlite_strict=True)

INSERT_ITEM = items.insert()
UPDATE_ITEM = items.update().where(items.c.row_id == bindparam("target_row_id")).values(
    **{name: bindparam(name) for name in (
        "natural_key", "row_version", "values_json", "body", "payload_hash", "updated_txn", "governance_json")})
INSERT_VERSION = item_versions.insert()
INSERT_IDENTITY = version_identity.insert()
INSERT_SOURCE = item_sources.insert()
INSERT_EFFECT = audit_effects.insert()
INSERT_TXN = txns.insert()
ADVANCE = collections.update().where(collections.c.collection_id == bindparam("target_collection_id")).values(
    generation=bindparam("generation"), audit_head=bindparam("audit_head"), updated_txn=bindparam("updated_txn"))
_PENDING = insert(projection_state)
PENDING = _PENDING.on_conflict_do_update(index_elements=[projection_state.c.path], set_={
    name: _PENDING.excluded[name] for name in ("pending_row_version", "pending_sha256", "state", "install_json")})
_META = insert(store_meta)
SET_META = _META.on_conflict_do_update(index_elements=[store_meta.c.key], set_={"value": _META.excluded.value})
