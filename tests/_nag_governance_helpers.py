"""Shared vault shaping for the S6 nag-governance tests.

One place, because five test modules need the same three things: a page whose
authored `check_by` has passed (the `prediction_window` family, which is in the
default attention union AND in the due-state projection, so one page exercises
every carrier), a scratch page to write to, and a write-path advisory candidate.

Kept out of `conftest.py` deliberately: these are the S6 slice's own fixtures,
not vault-wide ones, and a helper module a test imports by name is easier to
follow than an implicit fixture.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from exomem import corpus_aware
from exomem import find as find_module

INSIGHTS = "Knowledge Base/Notes/Insights"
SCRATCH = "Knowledge Base/Notes/Research/Infrastructure/nag-scratch.md"


def write(vault: Path, rel: str, text: str) -> str:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    find_module.clear_cache()
    return rel


def yesterday() -> str:
    return (dt.date.today() - dt.timedelta(days=1)).isoformat()


def overdue_prediction(
    vault: Path, slug: str = "nag-backlog", *, check_by: str | None = None
) -> str:
    """A page the `prediction_window` family flags, and nothing else."""
    return write(
        vault,
        f"{INSIGHTS}/{slug}.md",
        "---\n"
        f"title: {slug}\n"
        "type: insight\n"
        "status: active\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "---\n\n"
        "## Prediction\n\n"
        "- id: p1\n"
        f"- check_by: {check_by or yesterday()}\n\n"
        "The autovacuum backlog clears within a week.\n\n"
        "## Relations\n\n"
        "- supports [[Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization]]\n",
    )


def doubly_flagged(vault: Path, slug: str = "nag-double") -> str:
    """A page two families flag at once: `prediction_window` AND `relation_debt`.

    Same overdue prediction as `overdue_prediction`, minus the Relations
    section, which is what earns the second flag. Multi-flagged items are where
    the fused/component fingerprint split actually bites, so the compose rules
    need one.
    """
    return write(
        vault,
        f"{INSIGHTS}/{slug}.md",
        "---\n"
        f"title: {slug}\n"
        "type: insight\n"
        "status: active\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "---\n\n"
        "## Prediction\n\n"
        "- id: p1\n"
        f"- check_by: {yesterday()}\n\n"
        "An isolated claim with no relations at all.\n",
    )


def scratch_page(vault: Path) -> str:
    return write(
        vault,
        SCRATCH,
        "---\n"
        "title: nag scratch\n"
        "type: research-note\n"
        "project: infrastructure\n"
        "status: active\n"
        "created: 2026-08-01\n"
        "updated: 2026-08-01\n"
        "---\n\n"
        "# Nag scratch\n\n"
        "## Observations\n\n"
        "- [finding] The connection pool saturates above 400 concurrent readers.\n\n"
        "## Relations\n\n"
        "- supports [[Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization]]\n",
    )


def seed_page(vault: Path, name: str, body: str) -> str:
    path = f"{INSIGHTS}/{name}.md"
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: insight\nstatus: active\ncreated: 2026-08-16\n"
        "updated: 2026-08-16\ntags: []\n---\n"
        f"## Observations\n\n- [test] {body} ^{name}\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    return path


def advisory_candidate(vault: Path, name: str = "nag-counterpart") -> corpus_aware.DupCandidate:
    path = seed_page(vault, name, f"Counterpart signal for {name}.")
    return corpus_aware.DupCandidate(path=path, title=f"Existing {name}", cosine=0.86)


def emitted_advisories(vault: Path, kind: str = "near-duplicate") -> list[str]:
    """Whatever the write path would say about one near-duplicate counterpart."""
    target = seed_page(vault, "nag-editable", "Repeated body.")
    return corpus_aware.emit_write_advisories(
        vault,
        self_path=target,
        kind=kind,
        candidates=[advisory_candidate(vault)],
    )
