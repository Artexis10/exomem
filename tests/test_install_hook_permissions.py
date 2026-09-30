"""Hook installation accepts private groups without trusting shared writers."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import install_hook as hook_module

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes")


@pytest.fixture(autouse=True)
def _pin_install_hook_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def private_group(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    grp = pytest.importorskip("grp")
    pwd = pytest.importorskip("pwd")
    gid = tmp_path.stat().st_gid
    user = SimpleNamespace(pw_name="hook-owner", pw_gid=gid, pw_uid=os.geteuid())
    group = SimpleNamespace(gr_name=user.pw_name, gr_gid=gid, gr_mem=[])
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: user)
    monkeypatch.setattr(pwd, "getpwall", lambda: [user])
    monkeypatch.setattr(grp, "getgrgid", lambda _gid: group)
    return group


@pytest.mark.parametrize("umask", [0o002, 0o022], ids=["0002", "0022"])
@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("group_kind", ["primary-empty", "primary-sole-member"])
def test_install_accepts_private_group_writable_config(
    tmp_path: Path, private_group, client: str, umask: int, group_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_group.gr_name = "private-hook-group"
    if group_kind == "primary-sole-member":
        private_group.gr_mem = ["hook-owner"]
    pwd = pytest.importorskip("pwd")
    user = pwd.getpwuid(os.geteuid())
    other_user = SimpleNamespace(
        pw_name="another-user", pw_gid=private_group.gr_gid + 1, pw_uid=os.geteuid() + 1,
    )
    monkeypatch.setattr(pwd, "getpwall", lambda: [user, other_user])
    home = tmp_path / client
    home.mkdir()
    home.chmod(0o775)
    hooks = home / "hooks"
    hooks.mkdir()
    hooks.chmod(0o775)
    config = home / ("hooks.json" if client == "codex" else "settings.json")
    original = b'{"theme":"dark"}\n'
    config.write_bytes(original)
    config.chmod(0o664)

    previous = os.umask(umask)
    try:
        result = hook_module.install_hook(hook_dir=hooks, settings_path=config, client=client)
        assert result["wired"] is True
        assert json.loads(config.read_text())["theme"] == "dark"
        assert Path(result["backup"]).read_bytes() == original
        assert home.stat().st_mode & 0o777 == 0o775
        assert hooks.stat().st_mode & 0o777 == 0o775
        report = hook_module.check_hooks(clients=(client,), hook_dir=hooks, settings_path=config)
        checks = report["clients"][0]["checks"]
        assert next(row for row in checks if row["id"] == "config.file")["status"] == "pass"
        assert next(row for row in checks if row["id"] == "scripts.continuation")["status"] == "pass"
        assert hook_module.install_hook(
            hook_dir=hooks, settings_path=config, client=client,
        )["config_changed"] is False
    finally:
        os.umask(previous)


@pytest.mark.parametrize("umask", [0o002, 0o022], ids=["0002", "0022"])
@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("target", ["file", "parent", "ancestor", "hooks"])
@pytest.mark.parametrize("reason", [
    "shared-group", "other-write", "foreign-owner", "missing-group", "unavailable-group",
    "same-name", "sole-member", "shared-primary-empty", "shared-primary-sole-member",
    "missing-user", "unavailable-user", "missing-passwd", "unavailable-passwd",
])
def test_install_refuses_untrusted_config_and_directories(
    tmp_path: Path, private_group, monkeypatch: pytest.MonkeyPatch,
    client: str, umask: int, target: str, reason: str,
) -> None:
    home = tmp_path / client
    home.mkdir()
    config_parent = home / "config"
    config_parent.mkdir()
    hooks = home / "hooks"
    hooks.mkdir()
    config = config_parent / ("hooks.json" if client == "codex" else "settings.json")
    config.write_text('{"theme":"dark"}\n')
    path = {"file": config, "parent": config_parent, "ancestor": home, "hooks": hooks}[target]
    mode = 0o644 if target == "file" else 0o755
    if reason not in {"other-write", "foreign-owner"}:
        mode |= 0o020
        pwd = pytest.importorskip("pwd")
        grp = pytest.importorskip("grp")
        if reason == "shared-group":
            private_group.gr_mem = ["hook-owner", "another-user"]
        elif reason in {"same-name", "sole-member"}:
            user = pwd.getpwuid(os.geteuid())
            user.pw_gid = private_group.gr_gid + 1
            if reason == "sole-member":
                private_group.gr_name = "private-hook-group"
                private_group.gr_mem = [user.pw_name]
        elif reason.startswith("shared-primary-"):
            private_group.gr_name = "users"
            user = pwd.getpwuid(os.geteuid())
            if reason == "shared-primary-sole-member":
                private_group.gr_mem = [user.pw_name]
            other_user = SimpleNamespace(
                pw_name="another-user", pw_gid=private_group.gr_gid, pw_uid=os.geteuid() + 1,
            )
            monkeypatch.setattr(pwd, "getpwall", lambda: [user, other_user])
        else:
            error = KeyError if reason.startswith("missing-") else OSError

            def unavailable_database(*_args):
                raise error("account database unavailable")

            if reason.endswith("group"):
                monkeypatch.setattr(grp, "getgrgid", unavailable_database)
            elif reason.endswith("user"):
                monkeypatch.setattr(pwd, "getpwuid", unavailable_database)
            else:
                monkeypatch.setattr(pwd, "getpwall", unavailable_database)
    elif reason == "other-write":
        mode |= 0o002
    else:
        identity = path.stat()
        real_fstat = os.fstat

        def foreign_owned(fd):
            info = real_fstat(fd)
            if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                attrs = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                attrs["st_uid"] = os.geteuid() + 1
                return SimpleNamespace(**attrs)
            return info

        monkeypatch.setattr(os, "fstat", foreign_owned)
        # Ancestors already owned by another user are safe only when they
        # cannot be replaced; a writable foreign ancestor must be refused.
        if target == "ancestor":
            mode |= 0o020
    path.chmod(mode)
    before = config.read_bytes()

    previous = os.umask(umask)
    try:
        with pytest.raises(OSError, match="unsafe|writable|trusted|owned") as refused:
            hook_module.install_hook(hook_dir=hooks, settings_path=config, client=client)
        if target != "file":
            assert "chmod g-w" in str(refused.value)
        assert config.read_bytes() == before
        assert not list(config_parent.glob("*.backup-*"))
        report = hook_module.check_hooks(clients=(client,), hook_dir=hooks, settings_path=config)
        check_id = "scripts.continuation" if target == "hooks" else "config.file"
        assert next(
            row for row in report["clients"][0]["checks"] if row["id"] == check_id
        )["status"] == "fail"
    finally:
        os.umask(previous)


@pytest.mark.parametrize("umask", [0o002, 0o022], ids=["0002", "0022"])
def test_install_new_config_under_explicit_umask(tmp_path: Path, private_group, umask: int) -> None:
    previous = os.umask(umask)
    try:
        home = tmp_path / "client"
        home.mkdir()
        config = home / "settings.json"
        config.write_text("{}\n")
        assert stat.S_IMODE(home.stat().st_mode) == 0o777 & ~umask
        assert stat.S_IMODE(config.stat().st_mode) == 0o666 & ~umask
        assert hook_module.install_hook(hook_dir=home / "hooks", settings_path=config)["wired"]
    finally:
        os.umask(previous)


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_install_refuses_shared_primary_group_under_umask_0002(
    tmp_path: Path, private_group, monkeypatch: pytest.MonkeyPatch, client: str,
) -> None:
    pwd = pytest.importorskip("pwd")
    user = pwd.getpwuid(os.geteuid())
    private_group.gr_name = "users"
    assert private_group.gr_mem == []
    other_user = SimpleNamespace(
        pw_name="another-user", pw_gid=private_group.gr_gid, pw_uid=os.geteuid() + 1,
    )
    monkeypatch.setattr(pwd, "getpwall", lambda: [user, other_user])

    previous = os.umask(0o002)
    try:
        home = tmp_path / client
        home.mkdir()
        config = home / ("hooks.json" if client == "codex" else "settings.json")
        original = b'{"theme":"dark"}\n'
        config.write_bytes(original)
        assert stat.S_IMODE(home.stat().st_mode) == 0o775
        assert stat.S_IMODE(config.stat().st_mode) == 0o664
        assert home.stat().st_gid == config.stat().st_gid == user.pw_gid

        with pytest.raises(OSError, match="unsafe|writable|trusted"):
            hook_module.install_hook(
                hook_dir=home / "hooks", settings_path=config, client=client,
            )

        assert config.read_bytes() == original
        assert not list(home.glob("*.backup-*"))
        report = hook_module.check_hooks(
            clients=(client,), hook_dir=home / "hooks", settings_path=config,
        )
        assert next(
            row for row in report["clients"][0]["checks"] if row["id"] == "config.file"
        )["status"] == "fail"
    finally:
        os.umask(previous)
