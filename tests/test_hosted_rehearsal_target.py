from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def test_standalone_default_preserves_checked_release(monkeypatch):
    import hosted_rehearsal_target as target

    monkeypatch.delenv("SUBSTRATE_REHEARSAL_REPO", raising=False)
    monkeypatch.delenv("EXOMEM_REHEARSAL_RELEASE", raising=False)
    result = target.load_rehearsal_target()
    assert result["releaseVersion"] == "0.77.0"
    assert result["runtimeImage"].endswith(
        "73ab2439e653d490b800eb810c370e297da0efcad176557756b46b89b5c82172"
    )


def test_new_release_requires_companion_trust_registry(monkeypatch):
    import hosted_rehearsal_target as target

    monkeypatch.delenv("SUBSTRATE_REHEARSAL_REPO", raising=False)
    monkeypatch.setenv("EXOMEM_REHEARSAL_RELEASE", "99.0.0")
    with pytest.raises(ValueError, match="companion"):
        target.load_rehearsal_target()


def test_companion_resolution_is_read_only_and_scrubs_ambient_secrets(tmp_path, monkeypatch):
    import hosted_rehearsal_target as target

    monkeypatch.delenv("SUBSTRATE_REHEARSAL_REPO", raising=False)
    monkeypatch.delenv("EXOMEM_REHEARSAL_RELEASE", raising=False)
    expected = target.load_rehearsal_target()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/hosted-cluster-rehearsal.ts").touch()
    monkeypatch.setenv("SUBSTRATE_REHEARSAL_REPO", str(tmp_path))
    monkeypatch.setenv("EXOMEM_ADMIN_TOKEN", "private-sentinel")
    monkeypatch.setenv("DATABASE_URL", "private-sentinel")

    def run(argv, **kwargs):
        assert argv[-1] == "--describe-runtime-target"
        assert kwargs["cwd"] == tmp_path
        assert "EXOMEM_ADMIN_TOKEN" not in kwargs["env"]
        assert "DATABASE_URL" not in kwargs["env"]
        assert kwargs["timeout"] <= 30
        return SimpleNamespace(returncode=0, stdout=json.dumps(expected))

    monkeypatch.setattr(target.subprocess, "run", run)
    assert target.load_rehearsal_target() == expected


@pytest.mark.parametrize("mutation", ["wrong-release", "extra-field", "mutable-image", "missing-field"])
def test_malformed_companion_target_is_refused(tmp_path, monkeypatch, mutation):
    import hosted_rehearsal_target as target

    monkeypatch.delenv("SUBSTRATE_REHEARSAL_REPO", raising=False)
    monkeypatch.delenv("EXOMEM_REHEARSAL_RELEASE", raising=False)
    payload = target.load_rehearsal_target()
    if mutation == "wrong-release":
        payload["releaseVersion"] = "99.0.0"
    elif mutation == "extra-field":
        payload["extra"] = "ignored?"
    elif mutation == "mutable-image":
        payload["runtimeImage"] = "ghcr.io/artexis10/exomem:latest"
    else:
        del payload["sourceCommit"]
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/hosted-cluster-rehearsal.ts").touch()
    monkeypatch.setenv("SUBSTRATE_REHEARSAL_REPO", str(tmp_path))
    monkeypatch.setattr(target.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload)))
    with pytest.raises(ValueError, match="target"):
        target.load_rehearsal_target()
