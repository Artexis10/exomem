"""Provenance reporting: install origin, torch build, and the public/local split.

The regression these guard against: a service running from a wheel-backed venv
while a nearby checkout looked authoritative, plus a CUDA torch wheel silently
replaced by the default CPU wheel during an upgrade.
"""

from __future__ import annotations

import json

import pytest

from exomem import deploy_provenance


class _FakeDist:
    def __init__(self, direct_url: str | None):
        self._direct_url = direct_url

    def read_text(self, name: str) -> str | None:
        if name == "direct_url.json":
            return self._direct_url
        return None


def _editable_url(path: str) -> str:
    return json.dumps({"url": f"file:///{path}", "dir_info": {"editable": True}})


def _wheel_url() -> str:
    return json.dumps({"url": "https://pypi.org/simple/exomem", "dir_info": {}})


# --- install source classification -------------------------------------------


def test_editable_install_is_classified_editable(monkeypatch):
    monkeypatch.setattr(
        deploy_provenance,
        "distribution",
        lambda name: _FakeDist(_editable_url("C:/proj/exomem")),
    )
    source, root = deploy_provenance._install_source_and_root()
    assert source == "editable"
    assert root is not None
    assert root.as_posix().endswith("proj/exomem")


def test_wheel_install_is_classified_wheel(monkeypatch):
    monkeypatch.setattr(deploy_provenance, "distribution", lambda name: _FakeDist(_wheel_url()))
    source, root = deploy_provenance._install_source_and_root()
    assert source == "wheel"
    assert root is None


def test_missing_direct_url_reports_wheel(monkeypatch):
    """No direct_url.json is the normal shape for a plain wheel install."""
    monkeypatch.setattr(deploy_provenance, "distribution", lambda name: _FakeDist(None))
    source, _ = deploy_provenance._install_source_and_root()
    assert source == "wheel"


def test_unresolvable_distribution_reports_unknown(monkeypatch):
    """A wrong guess here is what caused the original confusion; say 'unknown'."""

    def _boom(name):
        raise RuntimeError("no metadata")

    monkeypatch.setattr(deploy_provenance, "distribution", _boom)
    source, root = deploy_provenance._install_source_and_root()
    assert source == "unknown"
    assert root is None


def test_malformed_direct_url_does_not_raise(monkeypatch):
    monkeypatch.setattr(deploy_provenance, "distribution", lambda name: _FakeDist("{not json"))
    source, _ = deploy_provenance._install_source_and_root()
    assert source == "wheel"


# --- torch build tag ----------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        ("2.12.0+cu132", True),
        ("2.12.0+cu121", True),
        ("2.5.0+rocm6.1", True),
        ("2.13.0", False),
        ("2.13.0+cpu", False),
        (None, False),
        ("", False),
    ],
)
def test_accelerated_detection(build, expected):
    assert deploy_provenance._is_accelerated(build) is expected


def test_torch_build_read_without_importing_torch(monkeypatch):
    """Metadata only. Importing torch would cost seconds and can fail outright."""
    seen: list[str] = []

    def _version(name: str) -> str:
        seen.append(name)
        return "2.12.0+cu132"

    monkeypatch.setattr(deploy_provenance, "version", _version)
    assert deploy_provenance._torch_build() == "2.12.0+cu132"
    assert seen == ["torch"]


def test_absent_torch_reports_none(monkeypatch):
    def _version(name: str):
        raise RuntimeError("not installed")

    monkeypatch.setattr(deploy_provenance, "version", _version)
    assert deploy_provenance._torch_build() is None
    assert deploy_provenance._is_accelerated(None) is False


# --- public vs local payload --------------------------------------------------


def test_public_payload_excludes_host_identifying_detail():
    """`/health` is unauthenticated and publicly reachable through the tunnel."""
    report = deploy_provenance.provenance(include_local=False)
    assert "interpreter" not in report
    assert "package_path" not in report
    assert "checkout" not in report

    blob = json.dumps(report)
    assert "C:\\Users" not in blob
    assert "/home/" not in blob


def test_local_payload_includes_interpreter():
    report = deploy_provenance.provenance(include_local=True)
    assert report["interpreter"]
    assert report["package_path"]
    # Everything published publicly is also present locally.
    public = deploy_provenance.provenance(include_local=False)
    assert set(public).issubset(set(report))


def test_provenance_reports_expected_keys():
    report = deploy_provenance.provenance()
    for key in ("version", "install_source", "revision", "torch", "accelerated", "extras"):
        assert key in report
    assert report["install_source"] in {"editable", "wheel", "unknown"}
    assert isinstance(report["extras"], list)


def test_revision_only_for_editable_installs(monkeypatch):
    monkeypatch.setattr(deploy_provenance, "distribution", lambda name: _FakeDist(_wheel_url()))
    monkeypatch.setattr(deploy_provenance, "_revision", lambda root: "deadbee")
    report = deploy_provenance.provenance()
    assert report["revision"] is None


def test_revision_tolerates_missing_git(monkeypatch, tmp_path):
    def _boom(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(deploy_provenance.subprocess, "run", _boom)
    assert deploy_provenance._revision(tmp_path) is None


def test_revision_none_for_missing_root():
    assert deploy_provenance._revision(None) is None
