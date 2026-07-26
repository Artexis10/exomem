from __future__ import annotations

from pathlib import Path

import pytest

from exomem import hosted_plugins


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_promotion_rejects_discovery_only_or_mocked_evidence() -> None:
    with pytest.raises(ValueError, match="content-bearing"):
        hosted_plugins.promote(REPO_ROOT, "claude", {"mocked": True})


def test_pending_records_are_not_distributed() -> None:
    distribution = hosted_plugins.distribution_manifest(REPO_ROOT)

    assert distribution == {"live_platforms": [], "cross_client_ready": False}
