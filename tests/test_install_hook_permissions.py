"""Hook installation accepts private groups without trusting shared writers."""

from __future__ import annotations

import errno
import json
import os
import stat
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import install_hook as hook_module

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes")


@pytest.fixture(autouse=True)
def _pin_install_hook_umask():
    cached = getattr(hook_module, "_private_group_for_user", None)
    if cached is not None:
        cached.cache_clear()
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)
        if cached is not None:
            cached.cache_clear()


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


@pytest.mark.parametrize("client,umask,group_kind", [
    ("codex", 0o002, "primary-empty"),
    ("claude", 0o022, "primary-empty"),
    ("codex", 0o022, "primary-sole-member"),
])
@pytest.mark.skipif(sys.platform != "linux", reason="Linux user-private-group exception")
def test_install_accepts_private_group_writable_config(
    tmp_path: Path, private_group, client: str, umask: int, group_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        assert stat.S_IMODE(config.stat().st_mode) == 0o644
        assert stat.S_IMODE(Path(result["backup"]).stat().st_mode) == 0o644
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


@pytest.mark.parametrize("target,reason", [
    *(('file', reason) for reason in (
        "shared-group", "other-write", "foreign-owner", "missing-group", "unavailable-group",
        "same-name", "sole-member", "shared-primary-empty", "shared-primary-sole-member",
        "missing-user", "unavailable-user", "missing-passwd", "unavailable-passwd",
    )),
    *( (target, reason) for target in ("parent", "ancestor", "hooks")
       for reason in ("shared-group", "foreign-owner") ),
])
def test_install_refuses_untrusted_config_and_directories(
    tmp_path: Path, private_group, monkeypatch: pytest.MonkeyPatch,
    target: str, reason: str,
) -> None:
    # Membership failures share one predicate; directory placements separately
    # exercise ancestor/leaf ownership and deployment before config mutation.
    client = "codex"
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

    with pytest.raises(OSError, match="unsafe|writable|trusted|owned") as refused:
        hook_module.install_hook(hook_dir=hooks, settings_path=config, client=client)
    assert "chmod g-w,o-w" in str(refused.value)
    assert config.read_bytes() == before
    assert not list(config_parent.glob("*.backup-*"))
    report = hook_module.check_hooks(clients=(client,), hook_dir=hooks, settings_path=config)
    check_id = "scripts.continuation" if target == "hooks" else "config.file"
    assert next(
        row for row in report["clients"][0]["checks"] if row["id"] == check_id
    )["status"] == "fail"


@pytest.mark.parametrize("umask", [0o002, 0o022], ids=["0002", "0022"])
@pytest.mark.skipif(sys.platform != "linux", reason="Linux user-private-group exception")
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
    assert private_group.gr_name == user.pw_name
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


@pytest.mark.parametrize("view", ["local-only", "missing-current", "wrong-name", "wrong-uid"])
def test_install_refuses_partial_passwd_enumeration(
    tmp_path: Path, private_group, monkeypatch: pytest.MonkeyPatch, view: str,
) -> None:
    pwd = pytest.importorskip("pwd")
    user = pwd.getpwuid(os.geteuid())
    if view == "local-only":
        private_group.gr_name = "domain users"
        entries = [SimpleNamespace(pw_name="local-user", pw_uid=user.pw_uid + 1, pw_gid=user.pw_gid + 1)]
    elif view == "missing-current":
        entries = []
    else:
        entries = [SimpleNamespace(
            pw_name=user.pw_name if view == "wrong-uid" else "another-name",
            pw_uid=user.pw_uid + 1 if view == "wrong-uid" else user.pw_uid,
            pw_gid=user.pw_gid + 1,
        )]
    monkeypatch.setattr(pwd, "getpwall", lambda: entries)
    home = tmp_path / "client"
    home.mkdir()
    config = home / "settings.json"
    original = b'{"theme":"dark"}\n'
    config.write_bytes(original)
    config.chmod(0o664)
    with pytest.raises(OSError, match="unsafe|writable|trusted"):
        hook_module.install_hook(hook_dir=home / "hooks", settings_path=config)
    assert config.read_bytes() == original
    assert not list(home.glob("*.backup-*"))


def test_install_refuses_mismatched_private_group_name(tmp_path: Path, private_group) -> None:
    private_group.gr_name = "domain users"
    home = tmp_path / "client"
    home.mkdir()
    home.chmod(0o775)
    with pytest.raises(OSError, match="unsafe|writable|trusted"):
        hook_module.install_hook(hook_dir=home / "hooks", settings_path=home / "settings.json")


def _set_extended_access_acl(path: Path) -> None:
    if sys.platform != "linux" or not hasattr(os, "setxattr"):
        pytest.skip("Linux POSIX access ACL xattrs required")
    # Linux ACL xattr v2: owner, named user, owning group, mask, other.
    # Use a mapped UID so this also runs in a single-UID sandbox namespace.
    execute = int(path.is_dir())
    acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, uid)
        for tag, permissions, uid in (
            (0x01, 6 | execute, 0xFFFFFFFF), (0x02, 7, os.geteuid()),
            (0x04, 4 | execute, 0xFFFFFFFF), (0x10, 6 | execute, 0xFFFFFFFF),
            (0x20, 4 | execute, 0xFFFFFFFF),
        )
    )
    try:
        os.setxattr(path, "system.posix_acl_access", acl)
    except OSError as error:
        if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}:
            pytest.skip("Filesystem does not support POSIX access ACLs")
        raise
    assert "system.posix_acl_access" in os.listxattr(path)
    assert path.stat().st_mode & 0o020


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("target", ["file", "hooks"])
def test_install_refuses_extended_access_acl(
    tmp_path: Path, private_group, client: str, target: str,
) -> None:
    home = tmp_path / client
    home.mkdir()
    hooks = home / "hooks"
    hooks.mkdir()
    config = home / ("hooks.json" if client == "codex" else "settings.json")
    original = b'{"theme":"dark"}\n'
    config.write_bytes(original)
    path = config if target == "file" else hooks
    _set_extended_access_acl(path)
    if target == "file":
        assert hook_module._safe_file_status(config)["mode_ok"] is False
        data, error = hook_module._read_json(config)
        assert data is None
        assert "chmod g-w,o-w" in error
    with pytest.raises(OSError, match="unsafe|writable|trusted") as refused:
        hook_module.install_hook(hook_dir=hooks, settings_path=config, client=client)
    assert "chmod g-w,o-w" in str(refused.value)
    assert config.read_bytes() == original
    assert not list(home.glob("*.backup-*"))
    report = hook_module.check_hooks(clients=(client,), hook_dir=hooks, settings_path=config)
    check_id = "config.file" if target == "file" else "scripts.continuation"
    assert next(row for row in report["clients"][0]["checks"] if row["id"] == check_id)["status"] == "fail"


def test_deployed_script_refuses_extended_access_acl(tmp_path: Path, private_group) -> None:
    home = tmp_path / "client"
    home.mkdir()
    hooks = home / "hooks"
    config = home / "settings.json"
    hook_module.install_hook(hook_dir=hooks, settings_path=config)
    script = hooks / hook_module._CONTINUATION_SCRIPT
    _set_extended_access_acl(script)
    report = hook_module.check_hooks(clients=("claude",), hook_dir=hooks, settings_path=config)
    assert next(row for row in report["clients"][0]["checks"] if row["id"] == "scripts.continuation")["status"] == "fail"


@pytest.mark.parametrize("platform", ["darwin", "freebsd14"])
@pytest.mark.parametrize("mode", [0o644, 0o664])
def test_non_linux_keeps_base_group_write_rule(
    tmp_path: Path, private_group, monkeypatch: pytest.MonkeyPatch, platform: str, mode: int,
) -> None:
    config = tmp_path / "settings.json"
    config.write_text("{}\n")
    config.chmod(mode)
    monkeypatch.setattr(sys, "platform", platform)
    assert hook_module._safe_file_status(config)["mode_ok"] is (mode == 0o644)


@pytest.mark.parametrize("error_code", [errno.ENOTSUP, errno.EOPNOTSUPP, errno.EACCES, errno.EIO])
@pytest.mark.skipif(sys.platform != "linux", reason="Linux user-private-group exception")
def test_acl_probe_errors_fail_closed_except_unsupported(
    tmp_path: Path, private_group, monkeypatch: pytest.MonkeyPatch, error_code: int,
) -> None:
    config = tmp_path / "settings.json"
    config.write_text("{}\n")
    config.chmod(0o664)

    def unavailable_xattrs(_target, **_kwargs):
        raise OSError(error_code, "xattr probe failed")

    monkeypatch.setattr(os, "listxattr", unavailable_xattrs)
    assert hook_module._safe_file_status(config)["mode_ok"] is (error_code in {errno.ENOTSUP, errno.EOPNOTSUPP})


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("umask", [0o002, 0o022], ids=["0002", "0022"])
@pytest.mark.skipif(sys.platform != "linux", reason="Linux POSIX default ACLs")
def test_install_default_acl_never_grants_write(
    tmp_path: Path, private_group, client: str, umask: int,
) -> None:
    home = tmp_path / client
    home.mkdir()
    config = home / ("hooks.json" if client == "codex" else "settings.json")
    original = b'{"theme":"dark"}\n'
    config.write_bytes(original)
    config.chmod(0o664)
    # Use a mapped UID even in single-UID sandboxes; inheritance still exercises
    # the named-user entry and the access ACL's write mask.
    acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, uid)
        for tag, permissions, uid in (
            (0x01, 7, 0xFFFFFFFF), (0x02, 7, os.geteuid()),
            (0x04, 5, 0xFFFFFFFF), (0x10, 7, 0xFFFFFFFF),
            (0x20, 5, 0xFFFFFFFF),
        )
    )
    try:
        os.setxattr(home, "system.posix_acl_default", acl)
    except OSError as error:
        if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}:
            pytest.skip("Filesystem does not support POSIX default ACLs")
        raise
    previous = os.umask(umask)
    try:
        try:
            result = hook_module.install_hook(
                hook_dir=home / "hooks", settings_path=config, client=client,
            )
        except OSError:
            assert config.read_bytes() == original
            assert not list(home.glob("*.backup-*"))
            return
        for path in (config, Path(result["backup"])):
            assert stat.S_IMODE(path.stat().st_mode) & 0o022 == 0
            if "system.posix_acl_access" in os.listxattr(path):
                access = os.getxattr(path, "system.posix_acl_access")
                entries = list(struct.iter_unpack("<HHI", access[4:]))
                assert all(not permissions & 2 for tag, permissions, _ in entries if tag == 0x10)
    finally:
        os.umask(previous)


@pytest.mark.parametrize("reason", ["other-write", "foreign-owner", "access-acl"])
def test_rewrite_refuses_unsafe_created_temp(
    tmp_path: Path, private_group, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    from exomem._hooks import exomem_continuation_checkpoint as safe

    config = tmp_path / "settings.json"
    original = b'{"theme":"dark"}\n'
    config.write_bytes(original)
    real_open = safe._open_secure_file_at
    real_fstat = os.fstat
    temporary_identity = None

    def unsafe_temp(directory, name, flags, mode=0o600):
        nonlocal temporary_identity
        fd = real_open(directory, name, flags, mode)
        if name.startswith(".settings.json.tmp-"):
            info = real_fstat(fd)
            temporary_identity = (info.st_dev, info.st_ino)
            if reason == "access-acl":
                _set_extended_access_acl(directory.path / name)
            elif reason == "other-write":
                os.fchmod(fd, 0o666)
        return fd

    def foreign_temp(fd):
        info = real_fstat(fd)
        if reason == "foreign-owner" and (info.st_dev, info.st_ino) == temporary_identity:
            attrs = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            attrs["st_uid"] = os.geteuid() + 1
            return SimpleNamespace(**attrs)
        return info

    monkeypatch.setattr(safe, "_open_secure_file_at", unsafe_temp)
    monkeypatch.setattr(os, "fstat", foreign_temp)
    with pytest.raises(OSError, match="unsafe|writable|owned"):
        hook_module._rewrite_hooks(config, lambda data: {**data, "changed": True})
    assert config.read_bytes() == original
    assert not list(tmp_path.glob("*.backup-*"))
    assert not list(tmp_path.glob(".settings.json.tmp-*"))


@pytest.mark.parametrize("acl", ["system.nfs4_acl", "system.cifs_acl"])
@pytest.mark.parametrize("target", ["path", "fd"])
@pytest.mark.skipif(sys.platform != "linux", reason="Linux private-group acceptance")
def test_group_write_refuses_network_acl(
    tmp_path: Path, private_group, monkeypatch: pytest.MonkeyPatch, acl: str, target: str,
) -> None:
    config = tmp_path / "settings.json"
    config.write_text("{}\n")
    config.chmod(0o664)
    monkeypatch.setattr(os, "listxattr", lambda _target, **_kwargs: [acl])
    if target == "path":
        assert hook_module._safe_file_status(config)["mode_ok"] is False
    else:
        with pytest.raises(OSError, match="unsafe|writable"):
            hook_module._snapshot_config(config)


@pytest.mark.parametrize("entrypoint", [
    "install_hook", "uninstall_hook", "install_all_hooks", "check_hooks",
])
@pytest.mark.skipif(sys.platform != "linux", reason="Linux private-group acceptance")
def test_entrypoint_rechecks_cached_private_group(
    tmp_path: Path, private_group, monkeypatch: pytest.MonkeyPatch, entrypoint: str,
) -> None:
    pwd = pytest.importorskip("pwd")
    user = pwd.getpwuid(os.geteuid())
    home = tmp_path / "client"
    home.mkdir()
    home.chmod(0o775)
    config = home / "settings.json"
    config.write_text("{}\n")
    hooks = home / "hooks"
    assert hook_module.install_hook(hook_dir=hooks, settings_path=config)["wired"] is True
    installed_config = config.read_bytes()
    other = SimpleNamespace(pw_name="another-user", pw_uid=user.pw_uid + 1, pw_gid=user.pw_gid)
    monkeypatch.setattr(pwd, "getpwall", lambda: [user, other])
    if entrypoint == "install_hook":
        with pytest.raises(OSError):
            hook_module.install_hook(hook_dir=hooks, settings_path=config)
    elif entrypoint == "uninstall_hook":
        report = hook_module.uninstall_hook(hook_dir=hooks, settings_path=config)
        assert report["success"] is False
        assert report["settings_error"] is not None
    elif entrypoint == "install_all_hooks":
        monkeypatch.setattr(hook_module, "SUPPORTED_CLIENTS", ("claude",))
        monkeypatch.setattr(hook_module, "_default_hook_dir", lambda _client: hooks)
        monkeypatch.setattr(hook_module, "_default_settings", lambda _client: config)
        report = hook_module.install_all_hooks()
        assert report["success"] is False
    else:
        report = hook_module.check_hooks(clients=("claude",), hook_dir=hooks, settings_path=config)
        assert next(
            row for row in report["clients"][0]["checks"] if row["id"] == "config.file"
        )["status"] == "fail"
    assert config.read_bytes() == installed_config
