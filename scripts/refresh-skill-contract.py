#!/usr/bin/env python
"""Refresh generated authoring projections and canonical skill-contract stamps."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from exomem import semantic_authoring, workflow_skills  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="verify the maintained projections and stamps"
    )
    args = parser.parse_args()
    concise = semantic_authoring.render_concise()
    schema = workflow_skills.WORKFLOW_SKILLS_DIR.parent
    targets = [schema / "references" / "semantic-authoring.md"]
    targets.extend(
        source
        for name, source in workflow_skills.contract_sources(schema)
        if name.startswith("workflow-skills/")
        and workflow_skills.is_standalone_authoring(source.parent.name)
    )
    changes = {}
    for target in targets:
        text = target.read_text(encoding="utf-8")
        start = text.index("<!-- exomem-semantic-authoring:")
        changes[target] = text[:start] + concise
    doc = REPO_ROOT / "docs" / "semantic-language.md"
    text = doc.read_text(encoding="utf-8")
    start = "<!-- BEGIN GENERATED SEMANTIC AUTHORING CONTRACT -->\n"
    end = "<!-- END GENERATED SEMANTIC AUTHORING CONTRACT -->"
    before, block = text.split(start, 1)
    _, after = block.split(end, 1)
    changes[doc] = before + start + concise + end + after
    stale = [path for path, text in changes.items() if path.read_text(encoding="utf-8") != text]
    if args.check:
        if stale:
            raise SystemExit(
                "authoring projections are stale: "
                + ", ".join(str(p.relative_to(REPO_ROOT)) for p in stale)
            )
    else:
        for path in stale:
            path.write_text(changes[path], encoding="utf-8", newline="\n")
        workflow_skills.refresh_skill_contract_stamps()
    digest = workflow_skills.validate_skill_contract()
    for name, source in workflow_skills.contract_sources(schema):
        if name.startswith("workflow-skills/"):
            workflow_skills.validate_contract_projection(source.parent.name, source.parent)
    workflow_skills.validate_contract_projection("exomem", schema, core=True)
    print(f"{'checked' if args.check else 'refreshed'} skill contract {digest}")


if __name__ == "__main__":
    main()
