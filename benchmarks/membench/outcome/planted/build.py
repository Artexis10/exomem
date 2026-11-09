"""Build one arm's vault for the planted wrong-note suite, then capture each case's recall packet.

Both arms receive the same pages with the same authored text, written through
Exomem's own MCP tools on a disposable vault. Only arm ``E`` also receives the
governance an agent would author when the correction arrives:

- ``superseded_rule``: ``supersede_note`` replaces the stale page with the correction.
- ``contradicted_by_source`` and ``refuted_claim``: the stale claim unit gets
  ``verdict: refuted`` and a ``contradicts`` relation to the correction.
- ``qualified_claim``: the stale claim unit gets ``verdict: qualified`` and an
  ``evidenced_by`` relation to the correction.

Arm ``P`` gets the identical pages without those writes. Governance is then the
only difference, and ``require_identical_text`` proves it before any answer: it
removes the builder's own governance lines from E's pages and refuses on any other
difference from P. The recall packet each arm serves for the case's task is what the
answering agent sees.

The builder reads cases authored by a separate lane. It never writes or edits
a key. ``membench outcome run planted`` drives it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from collection_ingest.mcp_session import McpSession

from exomem.vault import parse_frontmatter

from ..isolation import child_env, copy_state, fresh_vault

# The governance arm E authors per kind: (verdict, relation). None means supersession.
# Reason this is code: the registered design fixes these four correction kinds, and the verdict and
# relation keys are the shipped core pack's own keys for them, not a vocabulary a customer defines.
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


def neutral_unit_id(case_id: str, role: str) -> str:
    """A claim unit id that names no role: an id ending in "stale" told the answering agent
    which note to distrust, so it reached both arms and helped the one without governance."""
    return f"{case_id}-{hashlib.sha256(f'{case_id}:{role}'.encode()).hexdigest()[:6]}"


def claim_page(context: str, claim: str, unit_id: str) -> str:
    return f"{context}\n\n## Claim\n- id: {unit_id}\n\n{claim}\n"


def slug(text: str) -> str:
    return "-".join("".join(c.lower() if c.isalnum() else " " for c in text).split())[:48]


class Builder:
    def __init__(self, session: McpSession, log: Path) -> None:
        self.s = session
        self.log = log.open("a")
        #: Every page the builder wrote, in order. The product's own pages (its log) are not the builder's text.
        self.authored: list[str] = []

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
        self.authored.append(self.path_of(data))
        return self.authored[-1]

    async def case(self, case: dict, arm: str) -> dict:
        cid, kind, domain = case["case_id"], case["kind"], case["domain"]
        stale, corr = case["stale_note"], case["correction"]
        if GOVERNANCE[kind] is None and corr["form"] == "source":
            # ``supersede_note`` replaces a page with a note, so E could not govern this case: refuse, never skip.
            raise RuntimeError(
                f"{cid}: a superseded_rule correction must be a note, not a source; "
                "supersede_note cannot make a source the replacement"
            )
        stale_unit = neutral_unit_id(cid, "stale")
        stale_path = await self.note(
            stale["title"],
            claim_page(stale["context"], stale["claim"], stale_unit),
            domain,
            f"{cid}:stale",
        )
        corr_body = claim_page(
            f"{corr['source_label']}. {corr['context']}", corr["claim"], neutral_unit_id(cid, "correction")
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
            self.authored.append(corr_path)
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
            self.authored.append(corr_path)
        else:
            corr_path = await self.note(corr["title"], corr_body, domain, f"{cid}:correction")
        # What E's governance added to the pages, so the identical-text check can take exactly that out.
        added: dict | None = {"supersession": True} if arm == "E" and governance is None else None
        if arm == "E" and governance is not None:
            verdict, relation = governance
            target = corr_path.removesuffix(".md")
            inserted = f"- verdict: {verdict}\n- relations: {relation}: [[{target}]]\n"
            added = {"inserted": inserted}
            await self.call(
                "edit_note",
                {
                    "path": stale_path,
                    "why": f"Later evidence: {corr['title']}.",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": f"- id: {stale_unit}\n",
                        "new_string": f"- id: {stale_unit}\n{inserted}",
                    },
                },
                f"{cid}:verdict",
            )
        return {
            "case_id": cid,
            "stale_path": stale_path,
            "correction_path": corr_path,
            "governance": added,
        }

    async def packet(self, case: dict) -> dict:
        return await self.call(
            "recall_context",
            {"turn": case["task"], "client": "outcome-eval"},
            f"{case['case_id']}:recall",
        )


async def build(cases: list[dict], distractors: list[dict], arm: str, out: Path) -> None:
    """Build the arm's vault under ``out``, freeze ``out/built``, then capture each case's packet.

    ``out/built`` is the vault and state as built, frozen before any recall. A read warms the
    product's state, so no case may start from a copy taken after the packets. Every answer starts
    from a fresh copy of ``built``.
    """
    fresh_vault(out)
    async with McpSession(
        python=Path(sys.executable), state=out / "exomem-state", env=child_env(out)
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
        (out / "authored.json").write_text(json.dumps(b.authored, indent=2))
    copy_state(out, out / "built")
    async with McpSession(
        python=Path(sys.executable), state=out / "exomem-state", env=child_env(out)
    ) as session:
        b = Builder(session, out / "calls.jsonl")
        packets = out / "packets"
        packets.mkdir(exist_ok=True)
        for case in cases:
            (packets / f"{case['case_id']}.json").write_text(
                json.dumps(await b.packet(case), indent=2)
            )


# --------------------------------------------------------------------------
# The identical-text check.
# --------------------------------------------------------------------------

#: Frontmatter that differs between any two builds of the same text: identity and write times.
BUILD_KEYS = frozenset({"exomem_id", "created", "updated", "captured"})
#: What ``supersede_note`` adds: the stale page's status and pointer, the replacement's back-pointer.
SUPERSESSION_KEYS = {"stale": ("status", "superseded_by"), "correction": ("supersedes",)}


def authored_text(vault: Path, page: str, drop: tuple[str, ...], inserted: str | None) -> tuple:
    """A page's frontmatter and body without what any two builds differ in, and without ``drop`` and ``inserted``."""
    frontmatter, body, _ = parse_frontmatter((vault / page).read_text(encoding="utf-8"))
    if inserted is not None:
        if body.count(inserted) != 1:
            return ("governance text not found once", inserted)
        body = body.replace(inserted, "")
    return {k: v for k, v in frontmatter.items() if k not in BUILD_KEYS | set(drop)}, body


def require_identical_text(run_dir: Path) -> None:
    """Refuse unless E's and P's authored pages are equal once the builder's own governance is taken out.

    ``drop`` removes the supersession fields from both arms' pages, since P's page keeps its default
    ``status``; ``inserted`` is the exact text E's governance edit added to a stale page.
    """
    drop: dict[str, tuple[str, ...]] = {}
    inserted: dict[str, str] = {}
    for page in json.loads((run_dir / "E" / "pages.json").read_text()):
        governance = page["governance"]
        if governance and "inserted" in governance:
            inserted[page["stale_path"]] = governance["inserted"]
        elif governance:
            drop[page["stale_path"]] = SUPERSESSION_KEYS["stale"]
            drop[page["correction_path"]] = SUPERSESSION_KEYS["correction"]
    authored = {
        arm: json.loads((run_dir / arm / "authored.json").read_text()) for arm in ("E", "P")
    }
    if sorted(authored["E"]) != sorted(authored["P"]):
        raise RuntimeError("E and P authored different pages")
    differences = []
    for page in sorted(authored["E"]):
        e, p = (
            authored_text(run_dir / arm / "built" / "vault", page, drop.get(page, ()), ins)
            for arm, ins in (("E", inserted.get(page)), ("P", None))
        )
        if e != p:
            differences.append(page)
    if differences:
        raise RuntimeError(
            f"E and P differ beyond the builder's governance on {len(differences)} page(s); "
            f"first: {differences[:5]}"
        )
