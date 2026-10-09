"""D8: the nightly backup window, as [start_hour, end_hour) UTC.

It has no third-party imports, so the operator restore tools
(infra/scripts/cloud_restore_manifests.py) can import it on the node, where
cellctl's own dependencies are not installed.
"""

from __future__ import annotations

DEFAULT_BACKUP_WINDOW = (2, 5)


def parse_backup_window(raw: str) -> tuple[int, int]:
    """CELLCTL_BACKUP_WINDOW as the platform chart renders it: "start-end"."""

    start, end = raw.split("-")
    return int(start), int(end)


def within_backup_window(hour: int, window: tuple[int, int]) -> bool:
    # A window may cross midnight ("22-3" is hour >= 22 or hour < 3); start ==
    # end is empty, which the chart schema rejects.
    start, end = window
    if start < end:
        return start <= hour < end
    if start > end:
        return hour >= start or hour < end
    return False
