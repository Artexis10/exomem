"""Read the role's own public SSH rules out of `ufw show added` output."""

from __future__ import annotations

import ipaddress
import re

# The comment the base role writes on every public SSH allowance it adds. Only
# rules carrying it are the role's to retire; anything else (NetBird's wt0
# rule, rescue rules, uncommented rules) belongs to someone else.
MANAGED_COMMENT = "Exomem administrator SSH"

_MANAGED = re.compile(
    r"^ufw allow from (?P<source>\S+) to any port 22 proto tcp "
    + re.escape(f"comment '{MANAGED_COMMENT}'")
    + r"$"
)


def _network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    return ipaddress.ip_network(value, strict=False)


def retired_admin_ssh_sources(added: str, current: list[str]) -> list[str]:
    """Sources, as ufw shows them, of managed rules not in the current CIDRs."""
    keep = {_network(cidr) for cidr in current}
    retired = []
    for line in added.splitlines():
        match = _MANAGED.match(line.strip())
        if match and _network(match["source"]) not in keep:
            retired.append(match["source"])
    return retired


def admits_ssh_on_interface(added: str, interface: str) -> bool:
    """Whether a UFW rule admits inbound 22/tcp on the named interface."""
    rule = re.compile(
        rf"^ufw allow in on {re.escape(interface)} to any port 22(?: proto tcp)?"
        r"(?: comment '[^']*')?$"
    )
    return any(rule.match(line.strip()) for line in added.splitlines())


class FilterModule:
    def filters(self) -> dict[str, object]:
        return {
            "retired_admin_ssh_sources": retired_admin_ssh_sources,
            "admits_ssh_on_interface": admits_ssh_on_interface,
        }
