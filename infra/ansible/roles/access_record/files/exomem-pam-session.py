#!/usr/bin/python3
"""Optional OpenSSH 9.6 PAM metadata adapter. Never decides authentication."""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path


def authentication_metadata(value: str) -> dict[str, str]:
    methods = []
    keys = []
    try:
        for line in value.splitlines():
            parts = line.split()
            if not parts or len(parts[0]) > 64 or not parts[0].isascii():
                return {}
            methods.append(parts[0])
            if parts[0] == "publickey":
                if len(parts) < 3:
                    return {}
                blob = base64.b64decode(parts[2], validate=True)
                length = struct.unpack(">I", blob[:4])[0]
                key_type = blob[4 : 4 + length].decode("ascii")
                if not key_type or key_type != parts[1] or len(blob) <= 4 + length:
                    return {}
                # OpenSSH's maintained parser covers full key and certificate wire formats.
                valid = subprocess.run(
                    ["/usr/bin/ssh-keygen", "-l", "-f", "/dev/stdin"],
                    input=f"{parts[1]} {parts[2]}\n",
                    text=True,
                    timeout=1,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if valid.returncode != 0:
                    return {}
                keys.append((key_type, blob))
    except (ValueError, UnicodeError, struct.error, OSError, subprocess.SubprocessError):
        return {}
    if not methods:
        return {}
    result = {"EXOMEM_AUTHENTICATION_METHOD": "+".join(methods)}
    if len(keys) == 1:
        key_type, blob = keys[0]
        result["EXOMEM_FINGERPRINT"] = "SHA256:" + base64.b64encode(
            hashlib.sha256(blob).digest()
        ).decode().rstrip("=")
        # OpenSSH fixes this certificate wire-type suffix. The hash covers the certificate blob.
        result["EXOMEM_FINGERPRINT_KIND"] = (
            "certificate_blob" if key_type.endswith("-cert-v01@openssh.com") else "public_key_blob"
        )
    return result


def monitor_identity(parent_pid: int) -> str | None:
    try:
        # proc(5) fixes field 22 as starttime; comm can contain spaces and parentheses.
        tail = Path(f"/proc/{parent_pid}/stat").read_text().rpartition(")")[2].split()
        start = tail[19]
        if not start.isdecimal():
            return None
        return f"{parent_pid}:{start}"
    except (OSError, IndexError):
        return None


def main() -> None:
    try:
        action = os.environ.get("PAM_TYPE")
        # pam_exec fixes these two session callback names.
        if action not in ("open_session", "close_session"):
            return
        fields = {
            "SYSLOG_IDENTIFIER": "exomem-pam-session",
            "EXOMEM_ACCESS_RECORD": "1",
            "EXOMEM_ACTION": action,
            "EXOMEM_SOURCE_AT": datetime.now(UTC).isoformat(),
            "EXOMEM_EVENT_ID": str(uuid.uuid4()),
            "EXOMEM_IDENTITY": os.environ.get("PAM_USER", ""),
            "EXOMEM_SOURCE_ADDRESS": os.environ.get("PAM_RHOST", ""),
        }
        identity = monitor_identity(os.getppid())
        if identity:
            fields["EXOMEM_SESSION_ID"] = identity
        fields.update(authentication_metadata(os.environ.get("SSH_AUTH_INFO_0", "")))
        # Reject journal field separators; never write the environment or raw key.
        fields = {
            key: value
            for key, value in fields.items()
            if len(value) <= 2048 and not any(c in value for c in "\n\r\0")
        }
        subprocess.run(
            ["/usr/bin/logger", "--journald"],
            input="\n".join(f"{key}={value}" for key, value in fields.items()) + "\n",
            text=True,
            timeout=1,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


if __name__ == "__main__":
    main()
