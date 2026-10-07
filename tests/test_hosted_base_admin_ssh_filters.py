"""The base role's UFW parsing decides which public SSH rules it deletes.

A wrong parse either deletes a rule the operator still relies on (lockout) or
leaves a retired public allowance open, so it is pinned against real
`ufw show added` output.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "infra/ansible/roles/base/filter_plugins/admin_ssh.py"
)
_spec = importlib.util.spec_from_file_location("base_admin_ssh_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
admin_ssh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin_ssh)

ADDED = """\
Added user rules (see 'ufw status' for running firewall):
ufw allow from 192.0.2.1 to any port 22 proto tcp comment 'Exomem administrator SSH'
ufw allow from 198.51.100.0/24 to any port 22 proto tcp comment 'Exomem administrator SSH'
ufw allow from 2001:db8::/32 to any port 22 proto tcp comment 'Exomem administrator SSH'
ufw allow in on wt0 to any port 22 proto tcp comment 'Substrate NetBird administration'
ufw allow from 203.0.113.7 to any port 22 proto tcp
ufw allow from 203.0.113.8 to any port 22 proto tcp comment 'Operator rescue'
ufw allow from 10.42.0.0/16 comment 'Exomem K3s internal network'
"""


def test_retires_only_managed_rules_outside_the_current_cidrs() -> None:
    # 192.0.2.1/32 is shown by ufw without its prefix and must be retained.
    retired = admin_ssh.retired_admin_ssh_sources(ADDED, ["192.0.2.1/32"])

    assert retired == ["198.51.100.0/24", "2001:db8::/32"]


def test_empty_cidrs_retire_every_managed_rule_and_nothing_else() -> None:
    retired = admin_ssh.retired_admin_ssh_sources(ADDED, [])

    assert retired == ["192.0.2.1", "198.51.100.0/24", "2001:db8::/32"]


def test_detects_ssh_admitted_on_the_netbird_interface() -> None:
    assert admin_ssh.admits_ssh_on_interface(ADDED, "wt0")
    assert not admin_ssh.admits_ssh_on_interface(ADDED, "wt1")
    other_port = "ufw allow in on wt0 to any port 2222 proto tcp\n"
    assert not admin_ssh.admits_ssh_on_interface(other_port, "wt0")
    assert not admin_ssh.admits_ssh_on_interface("", "wt0")
