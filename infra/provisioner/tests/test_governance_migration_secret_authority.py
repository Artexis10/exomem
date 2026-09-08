from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

import pytest

from exomem_provisioner.adapters import KubernetesCellAdapter
from exomem_provisioner.driver import DriverRetryable
from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata
from exomem_provisioner.repository import ClaimConflict


def _fixture():
    owner = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "original-operation", 7)
    files = {name + ".json": b"{}" for name in ("control", "keyring", "serving-membership")}
    revision = hashlib.sha256(b"{}{}{}").hexdigest()
    secret = SimpleNamespace(
        metadata=SimpleNamespace(
            uid="secret-alpha",
            resource_version="7",
            deletion_timestamp=None,
            annotations={
                **owner.kubernetes_annotations,
                "exomem.io/recovery-envelope": "original-secret-envelope",
                "exomem.io/authorization-session-revision": revision,
            },
        ),
        data={key: base64.b64encode(value).decode() for key, value in files.items()},
    )

    class Core:
        def __init__(self):
            self.patches = []
            self.read_hook = lambda: None

        def read_namespaced_secret(self, name, namespace):
            assert namespace == owner.resource_name
            self.read_hook()
            return secret

        def patch_namespaced_secret(self, name, namespace, body):
            self.patches.append(body)

    core = Core()
    adapter = KubernetesCellAdapter(core_v1=core, apps_v1=None)
    arguments = {
        "recovery_envelope": "original-secret-envelope",
        "membership_epoch": 2,
        "membership_digest": "b" * 64,
        "revision": revision,
        "expected_revision": revision,
    }
    return owner, files, secret, core, adapter, arguments


async def _allow():
    return None


async def test_guarded_secret_patch_binds_uid_and_resource_version():
    owner, files, _secret, core, adapter, arguments = _fixture()
    await adapter.write_authorization_session_bundle(owner, files, **arguments, effect_guard=_allow)
    assert len(core.patches) == 1
    assert core.patches[0]["metadata"]["uid"] == "secret-alpha"
    assert core.patches[0]["metadata"]["resourceVersion"] == "7"


@pytest.mark.parametrize("omit", [False, True])
async def test_guarded_secret_requires_an_exact_predecessor_revision(omit):
    owner, files, _secret, core, adapter, arguments = _fixture()
    arguments.pop("expected_revision")
    if not omit:
        arguments["expected_revision"] = None
    with pytest.raises(MetadataConflict):
        await adapter.write_authorization_session_bundle(
            owner, files, **arguments, effect_guard=_allow
        )
    assert core.patches == []


async def test_secret_guard_runs_after_predecessor_read_and_before_patch():
    owner, files, _secret, core, adapter, arguments = _fixture()
    lost = False
    failure = ClaimConflict("claim lost during predecessor read")

    def lose():
        nonlocal lost
        lost = True

    async def guard():
        if lost:
            raise failure

    core.read_hook = lose
    with pytest.raises(ClaimConflict) as caught:
        await adapter.write_authorization_session_bundle(
            owner, files, **arguments, effect_guard=guard
        )
    assert caught.value is failure
    assert core.patches == []


@pytest.mark.parametrize("field", ["uid", "resource_version", "deletion_timestamp"])
async def test_guarded_secret_refuses_missing_identity_or_terminating_predecessor(field):
    owner, files, secret, core, adapter, arguments = _fixture()
    setattr(secret.metadata, field, "terminating" if field == "deletion_timestamp" else None)
    with pytest.raises(MetadataConflict):
        await adapter.write_authorization_session_bundle(
            owner, files, **arguments, effect_guard=_allow
        )
    assert core.patches == []


@pytest.mark.parametrize("status", [0, 408, 409, 429, 500, 503])
@pytest.mark.parametrize("method", ["read_namespaced_secret", "patch_namespaced_secret"])
async def test_guarded_secret_uncertainty_is_retryable_and_content_free(status, method):
    owner, files, _secret, core, adapter, arguments = _fixture()

    class ProviderFailure(Exception):
        pass

    def unavailable(*args, **kwargs):
        failure = ProviderFailure("private-provider-payload")
        failure.status = status
        raise failure

    setattr(core, method, unavailable)
    with pytest.raises(DriverRetryable) as caught:
        await adapter.write_authorization_session_bundle(
            owner, files, **arguments, effect_guard=_allow
        )
    assert str(caught.value) == "authorization session publication is temporarily unavailable"
    assert caught.value.__suppress_context__
    assert core.patches == []


@pytest.mark.parametrize("kind", ["envelope", "revision", "owner"])
async def test_guarded_secret_foreign_predecessor_is_terminal(kind):
    owner, files, secret, core, adapter, arguments = _fixture()
    field = {
        "envelope": "exomem.io/recovery-envelope",
        "revision": "exomem.io/authorization-session-revision",
        "owner": "exomem.io/operation-id",
    }[kind]
    secret.metadata.annotations[field] = "foreign"
    with pytest.raises(MetadataConflict):
        await adapter.write_authorization_session_bundle(
            owner, files, **arguments, effect_guard=_allow
        )
    assert core.patches == []
