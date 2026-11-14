"""The bounded catalogue build profile and observations before an upgrade attempt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .manifests import ResourceSettings


@dataclass(frozen=True)
class BuildConfig:
    resources: ResourceSettings
    timeout_seconds: int


@dataclass(frozen=True)
class BuildJob:
    name: str
    image: str
    node: str | None
    created_at: datetime | None
    running: bool = False
    succeeded: bool = False
    failed: bool = False
    pods_exist: bool = False

    @property
    def outcome(self) -> str:
        if self.succeeded:
            return "succeeded"
        if self.failed:
            return "failed"
        return "running" if self.running else "pending"
