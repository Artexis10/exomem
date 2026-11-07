"""Build one arm's vault for the planted wrong-note suite, then capture each case's recall packet.

Both arms receive the same pages with the same body text, written through
Exomem's own MCP tools on a disposable vault. Only arm ``E`` also receives the
governance an agent would author when the correction arrives:

- ``superseded_rule``: ``supersede_note`` replaces the stale page with the correction.
- ``contradicted_by_source`` and ``refuted_claim``: the stale claim unit gets
  ``verdict: refuted`` and a ``contradicts`` relation to the correction.
- ``qualified_claim``: the stale claim unit gets ``verdict: qualified`` and an
  ``evidenced_by`` relation to the correction.

Arm ``P`` gets the identical pages without those writes. Governance is then the
only difference, and the recall packet each arm serves for the case's task is
what the answering agent sees.

The builder reads cases authored by a separate lane. It never writes or edits
a key. ``membench outcome run planted`` drives it.
"""

from __future__ import annotations

import json
from pathlib import Path

from collection_ingest.mcp_session import McpSession

from ..isolation import child_env, fresh_vault, product_python_of

# The governance arm E authors per kind: (verdict, relation). None means supersession.
GOVERNANCE = {
    "superseded_rule": None,
    "contradicted_by_source": ("refuted", "contradicts"),
    "refuted_claim": ("refuted", "contradicts"),
    "qualified_claim": ("qualified", "evidenced_by"),
}


def _find(data, key: str):
    """The first value under ``key`` anywhere in a nested result."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        values = data.values()
    elif isinstance(data, list):
        values = data
    else:
        return None
    for value in values:
        found = _find(value, key)
        if found is not None:
            return found
    return None


def claim_page(context: str, claim: str, unit_id: str) -> str:
    return f"{context}\n\n## Claim\n- id: {unit_id}\n\n{claim}\n"


def slug(text: str) -> str:
    return "-".join("".join(c.lower() if c.isalnum() else " " for c in text).split())[:48]


class Builder:
    def __init__(self, session: McpSession, log: Path) -> None:
        self.s = session
        self.log = log.open("a")

    async def call(self, tool: str, args: dict, step: str) -> dict:
        outcome, data, record = await self.s.call(tool, args, journey="planted-build", step=step)
        self.log.write(json.dumps(record) + "\n")
        self.log.flush()
        if outcome != "ok":
            raise RuntimeError(
                f"{step}: {tool} -> {outcome} {record.get('error_code')}: {record.get('error_message')}"
            )
        return data

    @staticmethod
    def path_of(data: dict) -> str:
        for key in ("path", "new_path", "page"):
            if isinstance(data.get(key), str):
                return data[key]
        raise RuntimeError(f"no page path in the returned data {json.dumps(data)[:400]}")

    async def governed(self, tool: str, args: dict, step: str, *, review: str) -> dict:
        """The product's two-step write: validate, then commit with the draft's guards.

        A page that relates to nothing at write time commits with an explicit
        ``reviewed_none`` relation review, which is what the product asks an agent for.
        """
        draft = await self.call(tool, {**args, "validate_only": True}, f"{step}:validate")
        commit = dict(args)
        for key in ("draft_id", "draft_hash", "draft_token", "relation_review_hash"):
            value = _find(draft, key)
            if value is not None:
                commit[key] = value
        if "relation_review_hash" in commit:
            commit["relation_disposition"] = "reviewed_none"
            commit["relation_review_reason"] = review
        return await self.call(tool, commit, step)

    async def note(self, title: str, content: str, domain: str, step: str) -> str:
        data = await self.governed(
            "create_note",
            # No ``topics``: a second auto-registered topic on a fresh vault corrupts
            # source-taxonomy.yaml (product defect, fixed separately); both arms are affected alike.
            {"title": title, "content": content, "note_type": "insight", "sources": []},
            step,
            review="Written before any related page exists; no truthful typed relation applies yet.",
        )
        return self.path_of(data)

    async def case(self, case: dict, arm: str) -> dict:
        cid, kind, domain = case["case_id"], case["kind"], case["domain"]
        stale, corr = case["stale_note"], case["correction"]
        stale_unit = f"{cid}-stale"
        stale_path = await self.note(
            stale["title"],
            claim_page(stale["context"], stale["claim"], stale_unit),
            domain,
            f"{cid}:stale",
        )
        corr_body = claim_page(
            f"{corr['source_label']}. {corr['context']}", corr["claim"], f"{cid}-correction"
        )
        governance = GOVERNANCE[kind]
        if corr["form"] == "source":
            data = await self.call(
                "capture_source",
                {
                    "title": corr["title"],
                    "content": f"{corr['source_label']}. {corr['context']}\n\n{corr['claim']}\n",
                    "source_kind": "document",
                },
                f"{cid}:correction-source",
            )
            corr_path = self.path_of(data)
        elif arm == "E" and governance is None:
            data = await self.governed(
                "supersede_note",
                {
                    "old_path": stale_path,
                    "title": corr["title"],
                    "content": corr_body,
                    "note_type": "insight",
                    "reason": "The rule changed.",
                    "sources": [],
                },
                f"{cid}:supersede",
                review="The supersession itself links the two pages; no other typed relation applies.",
            )
            # The result names both pages; the replacement is the one that is not the stale page.
            corr_path = next(p for p in data["paths"] if p != stale_path)
        else:
            corr_path = await self.note(corr["title"], corr_body, domain, f"{cid}:correction")
        if arm == "E" and governance is not None:
            verdict, relation = governance
            target = corr_path.removesuffix(".md")
            await self.call(
                "edit_note",
                {
                    "path": stale_path,
                    "why": f"Later evidence: {corr['title']}.",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": f"- id: {stale_unit}\n",
                        "new_string": f"- id: {stale_unit}\n- verdict: {verdict}\n"
                        f"- relations: {relation}: [[{target}]]\n",
                    },
                },
                f"{cid}:verdict",
            )
        return {"case_id": cid, "stale_path": stale_path, "correction_path": corr_path}

    async def packet(self, case: dict) -> dict:
        return await self.call(
            "recall_context",
            {"turn": case["task"], "client": "outcome-eval"},
            f"{case['case_id']}:recall",
        )


async def build(
    cases: list[dict],
    distractors: list[dict],
    arm: str,
    out: Path,
    product_python: Path | None = None,
) -> None:
    """Build the arm's vault under ``out`` with the product interpreter, then capture each case's packet."""
    fresh_vault(out, product_python)
    env = child_env(out, product_python)
    async with McpSession(
        python=Path(product_python_of(product_python)), state=out / "exomem-state", env=env
    ) as session:
        b = Builder(session, out / "calls.jsonl")
        for i, d in enumerate(distractors):
            await b.note(
                d["title"],
                f"{d['context']}\n\n## Observations\n- [{slug(d['domain'])}] {d['claim']}\n",
                d["domain"],
                f"distractor-{i:03d}",
            )
        pages = [await b.case(case, arm) for case in cases]
        (out / "pages.json").write_text(json.dumps(pages, indent=2))
        packets = out / "packets"
        packets.mkdir(exist_ok=True)
        for case in cases:
            (packets / f"{case['case_id']}.json").write_text(
                json.dumps(await b.packet(case), indent=2)
            )
