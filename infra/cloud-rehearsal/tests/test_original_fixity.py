"""The original-fixity rehearsal runs the restore runbook's blocks as written, so it must find exactly those blocks.

The run itself needs a K3s cell and is exercised by the `original-fixity` workflow mode.
"""

from __future__ import annotations

import pytest

from cloud_rehearsal import original_fixity
from cloud_rehearsal.report import StepFailure


def test_the_shipped_runbook_yields_the_two_blocks_whose_lines_the_rehearsal_reads():
    # A runbook edit that adds, drops or reorders a block would otherwise surface only in a cluster run.
    check, restore = original_fixity.runbook_blocks(original_fixity.RUNBOOK.read_text())

    assert original_fixity.HASH_MATCHES in check and original_fixity.ORIGINAL_RESTORED not in check
    assert original_fixity.ORIGINAL_RESTORED in restore


def test_a_heading_inside_a_fence_does_not_end_the_section():
    text = (
        "## Restore one original\n\n```bash\n## a shell comment, not a heading\necho first\n```\n\n"
        "```bash\necho second\n```\n\n## Record\n\n```bash\necho another section\n```\n"
    )

    assert original_fixity.runbook_blocks(text) == ["## a shell comment, not a heading\necho first\n", "echo second\n"]


def test_a_section_with_another_block_count_stops_the_rehearsal():
    text = "## Restore one original\n\n```bash\necho only\n```\n\n## Record\n"

    with pytest.raises(StepFailure, match="1 closed bash blocks"):
        original_fixity.runbook_blocks(text)
