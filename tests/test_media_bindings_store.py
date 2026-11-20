"""Physical bindings follow canonical pages, including moves and unprojected writes."""

from __future__ import annotations

import hashlib
from contextlib import closing

from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401

from exomem import media_bindings, media_jobs
from exomem.vault_store import write


def test_unprojected_companions_are_included_and_audit_replay_retires_moved_paths(stored, writer):  # noqa: F811
    """A disk-first inventory would wrongly authorize eviction before the projector creates a companion."""
    binary = "Knowledge Base/Evidence/Archive/record.bin"
    companion = binary + ".md"
    digest = hashlib.sha256(b"canonical original").hexdigest()
    body = f"---\nevidence_file: {binary}\nbinary_sha256: {digest}\nbinary_size: 18\n---\n".encode()
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "test", puts=(write.PagePut(companion, body),)))
    assert not (stored / companion).exists()
    ledger = media_jobs.MediaJobStore(stored)
    assert media_bindings.rebuild(stored)
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.current(stored, conn)
        assert conn.execute("SELECT companion_hash FROM media_bindings WHERE binary_rel = ?",
                            (binary,)).fetchone()[0] == hashlib.sha256(body).hexdigest()
    moved = "Knowledge Base/Evidence/Archive/relocated.bin"
    moved_companion = moved + ".md"
    moved_body = body.replace(binary.encode(), moved.encode())
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "test", moves=(
        write.PageMove(companion, moved_companion, body=moved_body),)))
    with closing(ledger._connect(readonly=True)) as conn:
        assert not media_bindings.current(stored, conn)
    assert media_bindings.follow_store(stored)
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.current(stored, conn)
        assert conn.execute("SELECT companion_rel FROM media_bindings WHERE binary_rel = ?", (binary,)).fetchone() is None
        assert conn.execute("SELECT companion_rel FROM media_bindings WHERE binary_rel = ?",
                            (moved,)).fetchone()[0] == moved_companion


def test_a_bounded_audit_suffix_remains_unusable_until_every_row_is_followed(stored, writer):  # noqa: F811
    """A partial replay must not pass a current-source check after a multi-page commit."""
    ledger = media_jobs.MediaJobStore(stored)
    assert media_bindings.rebuild(stored)
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "test", puts=tuple(
        write.PagePut(f"Knowledge Base/Notes/audit-{index}.md", b"A new canonical page.\n") for index in range(3))))
    assert not media_bindings.follow_store(stored, batch=1)
    with closing(ledger._connect(readonly=True)) as conn:
        assert not media_bindings.current(stored, conn)
    assert not media_bindings.follow_store(stored, batch=1)
    assert media_bindings.follow_store(stored, batch=1)
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.current(stored, conn)
