from contextlib import closing

from exomem import vocabulary_delivery, vocabulary_recovery
from exomem.governance.principal import library_scope


def test_recovery_scans_a_fixed_window_and_rotates_hidden_work(tmp_path, monkeypatch):
    with library_scope():
        vocabulary_recovery.enqueue(tmp_path, "0", "Knowledge Base/Notes/0.md")
        with closing(vocabulary_recovery._connect(tmp_path)) as connection, connection:
            connection.executemany(
                "INSERT INTO vocabulary_recovery_jobs(job_id,path) VALUES (?,?)",
                [(str(index), f"Knowledge Base/Notes/{index}.md") for index in range(1, 1000)],
            )
    seen = []
    monkeypatch.setattr(vocabulary_delivery.vocabulary_review, "_visible", lambda root, item: seen.append(next(iter(item["target_versions"]))) or False)
    with library_scope():
        first = vocabulary_delivery.recover(tmp_path)
        second = vocabulary_delivery.recover(tmp_path)
    assert len(seen) == 8
    assert len(set(seen)) == 8
    assert first == second == {"state": "current", "processed": 0, "coverage": "bounded-pass"}
    assert "more" not in first


def test_recovery_cas_cannot_complete_a_newer_write(tmp_path):
    with library_scope():
        path = "Knowledge Base/Notes/example.md"
        vocabulary_recovery.enqueue(tmp_path, "old", path)
        old = vocabulary_recovery.page(tmp_path, limit=1)[0]
        assert vocabulary_recovery.claim(tmp_path, old)
        assert not vocabulary_recovery.claim(tmp_path, old)
        vocabulary_recovery.enqueue(tmp_path, "new", path)
        vocabulary_recovery.complete(tmp_path, old, continuation=None)
        jobs = vocabulary_recovery.page(tmp_path, limit=4)
    assert len(jobs) == 1
    assert jobs[0].key == "new"


def test_empty_recovery_does_not_create_a_store(tmp_path):
    with library_scope():
        assert vocabulary_recovery.page(tmp_path, limit=4) == ()
    assert not vocabulary_recovery.deferred_index.store_path(tmp_path).exists()
