from __future__ import annotations

from typing import Any

from ansible.plugins.callback import CallbackBase


DOCUMENTATION = r"""
name: content_free_json
type: stdout
short_description: Emit only aggregate Ansible convergence counters
description:
  - Emits no task arguments, host variables, stdout, stderr, or result payloads.
  - Intended only for the hosted two-run convergence gate.
"""


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "content_free_json"

    def v2_playbook_on_stats(self, stats: Any) -> None:
        counters: dict[str, dict[str, int]] = {}
        for host in sorted(stats.processed):
            summary = stats.summarize(host)
            counters[host] = {
                key: int(summary.get(key, 0))
                for key in ("changed", "failures", "unreachable", "ok", "skipped")
            }
        self._display.display(__import__("json").dumps({"stats": counters}, sort_keys=True))
