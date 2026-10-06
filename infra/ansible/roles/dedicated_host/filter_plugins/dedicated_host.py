"""Pure decisions for the dedicated_host role: which disks it may touch, and Tang keys."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

try:
    from ansible.errors import AnsibleFilterError
except ImportError:  # unit tests import this file without Ansible
    AnsibleFilterError = ValueError  # type: ignore[misc,assignment]


def _blkid_fields(output: str) -> dict[str, str]:
    """`blkid -p -o export` output, without the partition-table entry fields.

    A bare partition reports only PART_ENTRY_* fields: they describe where the
    partition sits, not anything stored in it.
    """
    fields = {}
    for line in output.splitlines():
        key, _, value = line.partition("=")
        if key and not key.startswith("PART_ENTRY_") and key != "DEVNAME":
            fields[key] = value
    return fields


def _mountpoints(node: dict[str, Any]) -> list[str]:
    found = [point for point in node.get("mountpoints") or [] if point]
    for child in node.get("children") or []:
        found += _mountpoints(child)
    return found


def dedicated_disk_verdicts(
    probes: list[dict[str, Any]], wipe: list[str], md_name: str
) -> list[dict[str, str]]:
    """Decide, per named data disk, whether the role may build the array on it.

    `ours`: already a member of this role's array. `blank`: no signature, no
    holder, no mount. `wipe`: a signature the inventory explicitly names for
    wiping, on a disk nothing holds or mounts. Anything else is `refuse`:
    the role never touches a disk that holds a filesystem, a partition table or
    another array unless told to, and never one in use even if told to.
    """
    verdicts = []
    for probe in probes:
        device = probe["device"]
        fields = _blkid_fields(probe.get("blkid", ""))
        tree = json.loads(probe["lsblk"])["blockdevices"][0]
        children = tree.get("children") or []
        mounts = _mountpoints(tree)
        label = fields.get("LABEL", "")
        signature = fields.get("TYPE") or (
            f"a {fields['PTTYPE']} partition table" if "PTTYPE" in fields else ""
        )
        if fields.get("TYPE") == "linux_raid_member" and (
            label == md_name or label.endswith(f":{md_name}")
        ):
            verdict, reason = "ours", f"member of {md_name}"
        elif not signature and not children and not mounts:
            verdict, reason = "blank", "no signature"
        elif children or mounts:
            verdict, reason = "refuse", "in use by another device or mount"
        elif device in wipe:
            verdict, reason = "wipe", f"holds {signature}; named for wiping"
        else:
            verdict, reason = "refuse", f"holds {signature}"
        verdicts.append({"device": device, "verdict": verdict, "reason": reason})
    return verdicts


def _tang_keys(jwks: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The signing and exchange keys of an escrowed Tang key set, validated.

    Errors name the shape, never a value: the input is private key material.
    """
    try:
        keys = json.loads(jwks)["keys"]
    except (TypeError, ValueError, KeyError):
        raise AnsibleFilterError("tang_keys must be a JSON key set with a keys list") from None
    if not isinstance(keys, list) or len(keys) != 2:
        raise AnsibleFilterError("tang_keys must hold exactly one signing and one exchange key")
    by_alg = {key.get("alg"): key for key in keys if isinstance(key, dict)}
    signing, exchange = by_alg.get("ES512"), by_alg.get("ECMR")
    for key, operation in ((signing, "sign"), (exchange, "deriveKey")):
        if (
            key is None
            or key.get("kty") != "EC"
            or key.get("crv") != "P-521"
            or operation not in key.get("key_ops", [])
            or not all(isinstance(key.get(member), str) for member in ("x", "y", "d"))
        ):
            raise AnsibleFilterError(
                "tang_keys needs a private ES512 signing key and a private ECMR exchange key"
            )
    return signing, exchange  # type: ignore[return-value]


def _thumbprint(key: dict[str, Any]) -> str:
    """RFC 7638 SHA-256 thumbprint of an EC key: what Clevis pins and Tang names files by."""
    members = {name: key[name] for name in ("crv", "kty", "x", "y")}
    canonical = json.dumps(members, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=").decode()


def tang_signing_thumbprint(jwks: str) -> str:
    return _thumbprint(_tang_keys(jwks)[0])


def tang_key_files(jwks: str) -> list[dict[str, str]]:
    return [
        {"name": f"{_thumbprint(key)}.jwk", "content": json.dumps(key, separators=(",", ":"))}
        for key in _tang_keys(jwks)
    ]


def clevis_tang_slots(listing: str, url: str) -> list[str]:
    """Key slots `clevis luks list` shows bound to Tang at exactly this URL."""
    slots = []
    for line in listing.splitlines():
        slot, _, rest = line.partition(":")
        pin, _, config = rest.strip().partition(" ")
        if not slot.strip().isdigit() or pin != "tang":
            continue
        try:
            bound = json.loads(config.strip().strip("'")).get("url")
        except ValueError:
            continue
        if bound == url:
            slots.append(slot.strip())
    return slots


class FilterModule:
    def filters(self) -> dict[str, object]:
        return {
            "dedicated_disk_verdicts": dedicated_disk_verdicts,
            "tang_signing_thumbprint": tang_signing_thumbprint,
            "tang_key_files": tang_key_files,
            "clevis_tang_slots": clevis_tang_slots,
        }
