#!/usr/bin/env python
"""Refresh canonical Exomem skill-contract stamps after changing skill guidance."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from exomem import workflow_skills  # noqa: E402


def main() -> None:
    digest = workflow_skills.refresh_skill_contract_stamps()
    workflow_skills.validate_skill_contract()
    print(f"refreshed skill contract {digest}")


if __name__ == "__main__":
    main()
