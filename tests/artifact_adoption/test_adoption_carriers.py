from __future__ import annotations

import json
from pathlib import Path
import pytest

pytestmark = pytest.mark.reads_repository_docs

ROOT = Path(__file__).resolve().parents[2]
def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8").lower()


def _active_capture_carriers(vault: Path) -> dict[str, str]:
    from exomem import commands, prominence

    return {
        "bootstrap": json.dumps(commands.op_bootstrap(vault), ensure_ascii=False).lower(),
        "prominence-balanced": prominence.CONTRACTS["balanced"].capture.lower(),
        "prominence-maximal": prominence.CONTRACTS["maximal"].capture.lower(),
        # The router delegates the engagement rules to this reference, so it is
        # the carrier that must teach adoption in each distribution.
        "scaffold": _text("src/exomem/_scaffold/_Schema/references/engagement.md"),
        "plugin-skill": _text("plugins/claude-code/skills/exomem/references/engagement.md"),
    }


def test_every_active_non_hosted_capture_carrier_teaches_draft_to_adoption(vault: Path) -> None:
    for name, carrier in _active_capture_carriers(vault).items():
        assert "generated draft" in carrier, name
        assert "selected" in carrier or "selection" in carrier, name
        assert "exact" in carrier and "bytes" in carrier, name
        assert "source" in carrier and "evidence" in carrier, name
        assert "mime" in carrier, name
        assert "no handle" in carrier or "without a handle" in carrier, name
        assert "handoff" in carrier and "non-committing" in carrier, name
        assert "delivery" in carrier and "record" in carrier, name
        assert "remote byte" in carrier, name


def test_every_active_non_hosted_capture_carrier_maps_adoption_authority(vault: Path) -> None:
    from exomem import commands
    from exomem.governance.principal import library_scope

    with library_scope():
        envelope = commands.op_bootstrap(vault)["engagement"]["envelope"]
    assert envelope["promotion_classes"]["typed_edge"] == "proactive_capture"
    assert envelope["promotion_classes"]["queue_acceptance"] == "link_acceptance"
    assert envelope["classes"]["restructure_execution"]["ceiling"] == "confirm-required"
    for name, carrier in _active_capture_carriers(vault).items():
        assert "proactive_capture" in carrier, name
        assert "structural_suggestions" in carrier, name
        assert "not write consent" in carrier or "not write confirmation" in carrier, name


def test_off_and_light_remain_explicit_request_only() -> None:
    from exomem import prominence

    for level in ("off", "light"):
        capture = prominence.CONTRACTS[level].capture.lower()
        assert ("only when" in capture or "unless" in capture) and "asks" in capture
        assert "generated draft" not in capture
        assert "proactive_capture" not in capture


def test_scaffold_and_deployed_claude_code_copies_match() -> None:
    assert (ROOT / "src/exomem/_scaffold/_Schema/SKILL.md").read_bytes() == (
        ROOT / "plugins/claude-code/skills/exomem/SKILL.md"
    ).read_bytes()
    for reference in ("operations.md", "write-scope.md"):
        assert (
            ROOT / "src/exomem/_scaffold/_Schema/references" / reference
        ).read_bytes() == (
            ROOT / "plugins/claude-code/skills/exomem/references" / reference
        ).read_bytes()
    assert (ROOT / "src/exomem/_hooks/exomem_capture_nudge.py").read_bytes() == (
        ROOT / "plugins/claude-code/hooks/exomem_capture_nudge.py"
    ).read_bytes()


def test_public_guides_teach_exact_adoption_and_honest_handoff() -> None:
    for path in ("QUICKSTART.md", "docs/ai-assistant-guide.md", "docs/prominence.md"):
        text = _text(path)
        assert "generated draft" in text, path
        assert "exact" in text and "bytes" in text, path
        assert "non-committing" in text and "handoff" in text, path
        assert "proactive_capture" in text, path
