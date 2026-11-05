"""Score `session-close` runs against the answer key and a blind judge.

Refuses to score while any selected case has no key. An independent writer supplies each object in
`scenarios/session-close/answer-keys/<case-id>.json`; generation never opens that folder.

    {
      "reference":        what the session settled and which page it belongs on, in plain words (the judge reads it),
      "must_change":      pages (relative to the knowledge base) that must differ after the session,
      "must_not_change":  seeded pages that must stay as they were,
      "recap":            "required", "forbidden" or "optional",
      "new_pages":        "none" (no page besides the recap) or "allowed"
    }

The recap is cited, not linked: a Source that a live compiled page names in `sources:` counts as compiled. The check
`recap_cited` needs no key field, because it reads the pages the run changed.

The capture judge sees turns, reference, page diffs and final required pages. The recall judge sees the fresh
question, reference, answer and successful page content. Neither view includes arm, model or compile diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import uuid
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from collection_ingest.mcp_session import classify
from epistemic.journeys.f27_replay import discover_agent_envelope, environment_floor
from jsonschema import Draft202012Validator

from . import checks, judge, runner
from .report import read_rows
from .session_close import DEFAULT_CLIENT_MODE, Case, client_mode_error, load_cases
from .store_report import paired_interval

KEY_FIELDS = {"reference", "must_change", "must_not_change", "recap", "new_pages"}
RECAP_RULES = ("required", "forbidden", "optional")
NEW_PAGE_RULES = ("none", "allowed")
#: The criterion IDs and key rules are the fixed scoring contract, not vault vocabulary.
CRITERIA = (
    {
        "id": "J1",
        "expect": True,
        "text": "Each change to an existing page records what the session settled, as the reference describes it, "
        "and states no fact the person did not give. True when no page changed.",
    },
    {
        "id": "J2",
        "expect": True,
        "text": "Each change to an existing page is appropriately scoped and preserves unrelated content and meaning. "
        "Adding a semantic unit required by the product contract, or a justified small structural change, is valid. "
        "True when no page changed.",
    },
    {
        "id": "J3",
        "expect": True,
        "text": "All material settled knowledge and semantic constraints in the reference appear on their rightful canonical pages. "
        "Include qualifications, rationale and temporal status. Missing knowledge or required canonical page work fails. "
        "Score recap production and recap citation separately; their absence alone does not fail this criterion. "
        "A recap or citation cannot replace required canonical content. True when no canonical update is owed.",
    },
)
# These criterion IDs are fixed by the scoring protocol.
RECALL_CRITERIA = (
    {"id": "R1", "expect": True, "text": "The answer correctly answers the fresh question using all relevant reference facts, without inventing facts."},
    {"id": "R2", "expect": True, "text": "Retrieved canonical content, excerpts or units contain the relevant reference facts and support the answer. Use each item's own provenance and lifecycle. Do not assume unseen content. A recap or locator alone fails."},
)
JUDGE_TIMEOUT_SECONDS = 300.0


def refuse_unkeyed(cases: Sequence[Case]) -> dict[str, dict]:
    """Stop before any page is read or any judge is called: a missing or malformed key means nothing can be scored."""
    keys = {}
    for case in cases:
        path = case.path.parent / "answer-keys" / f"{case.id}.json"
        try:
            key = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise SystemExit(f"refusing to score: {case.id} has no answer_key in {path}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise SystemExit(f"refusing to score: {case.id} answer_key cannot be read: {error}") from error
        if key is None:
            raise SystemExit(f"refusing to score: {case.id} has no answer_key in {path}")
        problems = key_problems(case, key)
        if problems:
            raise SystemExit(f"refusing to score: {case.id} answer_key: " + "; ".join(problems))
        keys[case.id] = key
    return keys


def key_problems(case: Case, key: object) -> list[str]:
    problems = []
    if not isinstance(key, dict) or set(key) != KEY_FIELDS:
        return [f"fields must be exactly {sorted(KEY_FIELDS)}"]
    if not isinstance(key["reference"], str) or not key["reference"].strip():
        problems.append("reference must be a non-empty string")
    if key["recap"] not in RECAP_RULES:
        problems.append(f"recap must be one of {RECAP_RULES}")
    if key["new_pages"] not in NEW_PAGE_RULES:
        problems.append(f"new_pages must be one of {NEW_PAGE_RULES}")
    seeded = {p.relative_to(case.seed).as_posix() for p in case.seed.rglob("*.md")}
    for name in ("must_change", "must_not_change"):
        listed = key[name]
        if not isinstance(listed, list) or not all(isinstance(p, str) for p in listed):
            problems.append(f"{name} must be a list of page paths")
        elif set(listed) - seeded:
            problems.append(f"{name} names pages that are not in the seed: {sorted(set(listed) - seeded)}")
    return problems


def check_row(key: dict, row: dict) -> list[checks.Outcome]:
    """Deterministic checks of one run's snapshot diff against the key."""
    seen = row["observed"]
    changed = set(seen["changed"])
    touched = changed | set(seen["removed"])
    recaps = seen["recaps"]
    compiled = {p: page["cites"] for p, page in seen["pages"].items() if page["compiled"]}
    other_pages = [p for p in seen["created"] if p not in recaps]

    def outcome(name: str, held: bool, detail: str) -> checks.Outcome:
        return checks.Outcome(name, checks.PASS if held else checks.FAIL, detail)

    missing = sorted(set(key["must_change"]) - changed)
    wrong = sorted(set(key["must_not_change"]) & touched)
    uncited = sorted(p for p, cites in compiled.items() if not cites)
    recap_held = {"required": bool(recaps), "forbidden": not recaps, "optional": True}[key["recap"]]
    return [
        outcome("must_change", not missing, f"not changed: {missing}" if missing else "all changed"),
        outcome("must_not_change", not wrong, f"touched: {wrong}" if wrong else "none touched"),
        outcome("recap", recap_held, f"{len(recaps)} recap(s); the key says {key['recap']}"),
        # Not applicable without a recap or a changed compiled page: coverage by citation has nothing to count.
        checks.Outcome("recap_cited", *cited_verdict(recaps, compiled, uncited)),
        outcome(
            "new_pages",
            key["new_pages"] == "allowed" or not other_pages,
            f"pages besides the recap: {other_pages}" if other_pages else "none besides the recap",
        ),
    ]


def cited_verdict(recaps: dict, compiled: dict, uncited: list[str]) -> tuple[str, str]:
    """The verdict and detail of `recap_cited`: every changed compiled page names a written recap in its `sources:`."""
    if not recaps or not compiled:
        return checks.NOT_APPLICABLE, "no recap written" if not recaps else "no compiled page changed"
    if uncited:
        return checks.FAIL, f"changed compiled pages that cite no recap: {uncited}"
    return checks.PASS, "every changed compiled page cites a recap"


def judge_view(case: Case, key: dict, row: dict) -> str:
    """The same view for every arm: the person's turns, the reference, and what happened to the pages."""
    seen = row["observed"]
    turns = "\n".join(f"{n}. {t}" for n, t in enumerate(case.turns, start=1))
    changes = [f"### Changed: {p}\n{diff}" for p, diff in seen["changed"].items()]
    changes += [f"### Removed: {p}" for p in seen["removed"]]
    changes += [
        f"### Created: {p}\n{text}" for p, text in seen["created"].items() if p not in seen["recaps"]
    ]
    final_pages = row.get("capture_after", {})
    required = "\n\n".join(
        f"### {path}\n{final_pages.get(path, {}).get('text', 'Page unavailable.')}"
        for path in key["must_change"]
    )
    return (
        f"WHAT THE PERSON SAID, IN ORDER\n{turns}\n\n"
        "INDEPENDENT REFERENCE (semantic truth and workflow obligations)\n"
        "J1–J3 assess canonical knowledge, factual correctness and justified preservation. "
        "Separate deterministic checks assess recap production and required recap citation. "
        "A missing new recap citation alone does not fail J1–J3. "
        "Unsupported knowledge and lost unrelated content still fail the applicable criterion.\n"
        f"{key['reference']}\n\n"
        "PAGE CHANGES THE SESSION LEFT, AS DIFFS\n" + ("\n\n".join(changes) or "No page changed.")
        + (f"\n\nFINAL REQUIRED PAGES\n{required}" if required else "")
    )


def judge_prompt(view: str, criteria: tuple[dict, ...] = CRITERIA) -> str:
    listed = "\n".join(f"- {c['id']}: {c['text']}" for c in criteria)
    return (
        "Read the independent reference and the captured evidence. For each criterion, answer true if it holds "
        "and false if it does not, with a one-line reason. Judge only the evidence shown, including missing "
        "material required by the reference.\n\nCRITERIA\n" + listed + "\n\nCASE\n" + view
    )


def material_changes(seen: dict) -> bool:
    return bool(seen["changed"] or seen["removed"] or any(path not in seen["recaps"] for path in seen["created"]))


def judge_row(case: Case, key: dict, row: dict, *, model: str, executable: Path | None, scratch: Path, answer: dict | None = None, automatic_judge: bool = False) -> list[checks.Outcome]:
    seen = row["observed"]
    if not material_changes(seen):
        return [
            checks.Outcome(c["id"], checks.FAIL if c["id"] == "J3" and key["must_change"] else checks.PASS,
                           "required page work is missing" if c["id"] == "J3" and key["must_change"] else "no page changed")
            for c in CRITERIA
        ]
    outcomes = (
        run_judge(judge_view(case, key, row), CRITERIA, model=model, executable=executable, scratch=scratch)
        if automatic_judge else checks.judged_outcomes(CRITERIA, answer)
    )
    if set(key["must_change"]) - set(seen["changed"]):
        outcomes = [checks.Outcome(o.check, checks.FAIL, "required page work is missing") if o.check == "J3" else o for o in outcomes]
    return outcomes


def run_judge(view: str, criteria: tuple[dict, ...], *, model: str, executable: Path | None, scratch: Path) -> list[checks.Outcome]:
    """The legacy Claude judge runs only when explicitly selected."""
    assert executable is not None
    empty_mcp = scratch / "empty-mcp.json"
    empty_mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
    env, _removed = environment_floor(os.environ)
    env = {k: v for k, v in env.items() if not k.startswith("EXOMEM_")}
    try:
        done = subprocess.run(  # noqa: S603
            runner.judge_argv(
                executable,
                judge_prompt(view, criteria),
                judge.answer_schema(criteria),
                empty_mcp=empty_mcp,
                model=model,
            ),
            cwd=str(scratch),
            env=env,
            capture_output=True,
            text=True,
            timeout=JUDGE_TIMEOUT_SECONDS,
        )
        answer = judge.parse_answer(done.stdout, criteria)
    except (OSError, subprocess.TimeoutExpired):
        answer = None
    return checks.judged_outcomes(criteria, answer)


def content_evidence(tool: str, data: object) -> tuple[bool, list[dict]]:
    """Project only source-defined content fields; anchors, titles and enclosing-map paths are not prose provenance."""
    # These fields and tool names are fixed wire schemas in semantic_unit_read, retrieval_models,
    # working_set, context_views and context_pack; no vocabulary or meaning is inferred here.
    lifecycle_fields = ("status", "updated", "supersedes", "superseded_by", "parent_status", "parent_updated", "parent_superseded_by", "lifecycle", "verdict", "check_by", "as_of")
    bounds_fields = ("body_truncated", "window", "truncation", "truncated_before", "truncated_after", "matched_units_truncated", "mixed_units_truncated", "continuation", "missing", "stale", "overflow")
    evidence = []

    def obj(value: object) -> dict:
        return value if isinstance(value, dict) else {}

    def add(path: object, text: object, *owners: dict, bounds: tuple[dict, ...] = ()) -> None:
        if not (isinstance(path, str) and path and isinstance(text, str) and text.strip()):
            return
        item = {"path": path, "text": text}
        lifecycle = {name: owner[name] for owner in owners for name in lifecycle_fields if name in owner}
        limits = {name: owner[name] for owner in bounds for name in bounds_fields if name in owner}
        if lifecycle:
            item["lifecycle"] = lifecycle
        if limits:
            item["bounds"] = limits
        evidence.append(item)

    if tool == "read_memory" and isinstance(data, dict):
        parent = obj(data.get("parent"))
        if "status" in data and isinstance(parent.get("path"), str):
            if data["status"] == "found":
                unit, context = obj(data.get("unit")), obj(data.get("parent_context"))
                if not isinstance(unit.get("content"), str):
                    return False, []
                add(parent["path"], unit.get("content"), parent, unit, bounds=(data,))
                add(parent["path"], context.get("markdown"), parent, bounds=(data, context))
            return True, evidence
        if isinstance(data.get("path"), str):
            if "body" in data and not isinstance(data["body"], str):
                return False, []
            add(data["path"], data.get("body"), obj(data.get("frontmatter")), bounds=(data,))
            return True, evidence
        return False, []
    if tool == "search_memory":
        envelope = obj(data)
        hits = data if isinstance(data, list) else envelope.get("hits")
        pack = obj(envelope.get("pack"))
        known = isinstance(hits, list) or isinstance(pack.get("claims"), dict) or isinstance(pack.get("semantic_units"), dict)
        for hit in hits if isinstance(hits, list) else []:
            hit = obj(hit)
            if isinstance(hit.get("parent_path"), str):
                known &= all(isinstance(hit[name], str) for name in ("content", "excerpt") if name in hit)
                add(hit["parent_path"], hit.get("content") or hit.get("excerpt"), hit, bounds=(envelope, hit))
            elif isinstance(hit.get("path"), str):
                known &= "excerpt" not in hit or isinstance(hit["excerpt"], str)
                add(hit["path"], hit.get("excerpt"), hit, bounds=(envelope, hit))
            else:
                known = False
        for path, claim in obj(pack.get("claims")).items():
            claim = obj(claim)
            known &= isinstance(claim.get("lede"), str) and isinstance(claim.get("sections"), list)
            pieces = [claim.get("lede"), *(claim.get("sections") if isinstance(claim.get("sections"), list) else [])]
            add(path, "\n".join(piece for piece in pieces if isinstance(piece, str)), claim, bounds=(envelope, pack))
        for group in obj(pack.get("semantic_units")).values():
            group = obj(group)
            parent = obj(group.get("parent"))
            known &= isinstance(parent.get("path"), str) and isinstance(group.get("units"), list)
            for unit in group.get("units") if isinstance(group.get("units"), list) else []:
                unit = obj(unit)
                known &= isinstance(unit.get("excerpt"), str)
                add(parent.get("path"), unit.get("excerpt"), parent, unit, bounds=(envelope, pack))
        return known, evidence
    if tool in ("recall_context", "activate_context") and isinstance(data, dict):
        map_ = obj(data.get("map"))
        known = any(isinstance(data.get(name), list) for name in ("units", "current_state", "pointers", "anchors")) or isinstance(map_.get("items"), list)
        for unit in data.get("units") if isinstance(data.get("units"), list) else []:
            unit = obj(unit)
            provenance = obj(unit.get("provenance"))
            known &= isinstance(provenance.get("path"), str) and isinstance(unit.get("text"), str)
            add(provenance.get("path"), unit.get("text"), provenance, unit, bounds=(data,))
        for state in data.get("current_state") if isinstance(data.get("current_state"), list) else []:
            state = obj(state)
            known &= isinstance(state.get("path"), str) and isinstance(state.get("statement"), str)
            add(state.get("path"), state.get("statement"), state, bounds=(data,))
        for item in map_.get("items") if isinstance(map_.get("items"), list) else []:
            item = obj(item)
            if "state" not in item:
                continue
            state = obj(item["state"])
            known &= isinstance(state.get("path"), str) and isinstance(state.get("statement"), str)
            add(state.get("path"), state.get("statement"), state, bounds=(data, map_))
        return known, evidence
    return False, []


def recall_evidence(row: dict, key: dict | None = None) -> tuple[str, list[dict]]:
    """Content actually served from canonical pages; unreadable or unknown transport remains unknown."""
    from exomem.kbdir import kb_prefix

    fresh = row.get("fresh")
    mode = row.get("client_mode", DEFAULT_CLIENT_MODE)
    if mode == "tool-only" and (client_mode_error(mode, row) or not fresh or fresh.get("client_mode", DEFAULT_CLIENT_MODE) != mode or client_mode_error(mode, fresh)):
        return checks.UNKNOWN, []
    if not fresh or fresh.get("error") or (fresh.get("server_connected") or {}).get(checks.SERVER) != "connected":
        return checks.UNKNOWN, []
    if not fresh.get("answer") or not fresh.get("session_id"):
        return checks.UNKNOWN, []
    evidence, unreadable = [], False
    # The public retrieval names are fixed API tokens. All four can serve canonical content.
    retrieval = {f"mcp__{checks.SERVER}__{name}" for name in ("read_memory", "search_memory", "recall_context", "activate_context")}
    for call in fresh.get("calls", []):
        if call.get("name") not in retrieval:
            continue
        if call.get("is_error") is not False or call.get("result") is None:
            unreadable = True
            continue
        payload = call["result"]
        if isinstance(payload, list):
            if not payload or not all(isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str) for part in payload):
                unreadable = True
                continue
            payload = "\n".join(part["text"] for part in payload)
        try:
            payload = json.loads(payload) if isinstance(payload, str) else payload
        except ValueError:
            unreadable = True
            continue
        outcome, data, _code, _message = classify(payload)
        if outcome != "ok":
            unreadable = True
            continue
        known, shown = content_evidence(call["name"].removeprefix(f"mcp__{checks.SERVER}__"), data)
        unreadable |= not known
        evidence.extend(shown)
    if key is not None:
        # Both updated and preserved seed pages can support recall; the blind reference judge decides relevance.
        canonical = set(key["must_change"]) | set(key["must_not_change"])
        evidence = [item for item in evidence if item["path"].removeprefix(kb_prefix()) in canonical]
    if not evidence and unreadable:
        return checks.UNKNOWN, []
    return checks.PASS if evidence else checks.FAIL, evidence


def recall_view(key: dict, row: dict, pages: list[dict]) -> str:
    fresh = row["fresh"]
    return (
        f"FRESH QUESTION\n{fresh['question']}\n\nREFERENCE\n{key['reference']}\n\n"
        f"ANSWER\n{fresh['answer']}\n\nRETRIEVED CANONICAL CONTENT\n{json.dumps(pages, ensure_ascii=False)}"
    )


def score_recall(key: dict, row: dict, *, judge_model: str, executable: Path | None, scratch: Path, answer: dict | None = None, automatic_judge: bool = False) -> dict:
    if not row.get("recall_enabled"):
        return {"verdict": checks.NOT_APPLICABLE, "judged": []}
    status, pages = recall_evidence(row, key)
    if status != checks.PASS:
        return {"verdict": status, "judged": []}
    judged = (
        run_judge(recall_view(key, row, pages), RECALL_CRITERIA, model=judge_model, executable=executable, scratch=scratch)
        if automatic_judge else checks.judged_outcomes(RECALL_CRITERIA, answer)
    )
    return {"verdict": checks.verdict(None, [], judged), "judged": [o.__dict__ for o in judged]}


def score_row(case: Case, key: dict, row: dict, *, judge_model: str, executable: Path | None, scratch: Path, answers: dict | None = None, automatic_judge: bool = False) -> dict:
    answers = answers or {}
    recall = score_recall(key, row, judge_model=judge_model, executable=executable, scratch=scratch, answer=answers.get("recall"), automatic_judge=automatic_judge)
    if client_mode_error(row.get("client_mode", DEFAULT_CLIENT_MODE), row) is not None:
        return {"deterministic": [], "judged": [], "verdict": checks.UNKNOWN, "recall": recall}
    deterministic = check_row(key, row)
    judged = judge_row(case, key, row, model=judge_model, executable=executable, scratch=scratch, answer=answers.get("capture"), automatic_judge=automatic_judge)
    return {
        "deterministic": [o.__dict__ for o in deterministic],
        "judged": [o.__dict__ for o in judged],
        "verdict": checks.verdict(None, deterministic, judged),
        "recall": recall,
    }


def native_views(case: Case, key: dict, row: dict) -> dict[str, tuple[str, tuple[dict, ...]]]:
    """Only stages that need semantic judgments; transport failures retain their deterministic unknown state."""
    views = {}
    if client_mode_error(row.get("client_mode", DEFAULT_CLIENT_MODE), row) is None and material_changes(row["observed"]):
        views["capture"] = (judge_prompt(judge_view(case, key, row)), CRITERIA)
    status, pages = recall_evidence(row, key)
    if row.get("recall_enabled") and status == checks.PASS:
        views["recall"] = (judge_prompt(recall_view(key, row, pages), RECALL_CRITERIA), RECALL_CRITERIA)
    return views


def run_identity(row: dict) -> tuple:
    """Historical session-close records belong to plugin mode; mode is part of run identity."""
    return (*(row[name] for name in ("scenario", "repeat", "model", "arm")), row.get("client_mode", DEFAULT_CLIENT_MODE))


def prepare_native(runs: Sequence[Path], *, out: Path, case_dir: Path | None = None) -> int:
    """Export blind prompts separately from the run mapping; start no model."""
    cases = {c.id: c for c in load_cases("all", case_dir=case_dir)}
    rows, _replaced = read_rows(runs, identity=run_identity)
    keys = refuse_unkeyed([cases[sid] for sid in dict.fromkeys(r["scenario"] for r in rows)])
    items, mapping = [], {}
    for row in rows:
        for stage, (prompt, criteria) in native_views(cases[row["scenario"]], keys[row["scenario"]], row).items():
            item_id = uuid.uuid4().hex
            items.append({"id": item_id, "prompt": prompt, "answer_schema": judge.answer_schema(criteria)})
            mapping[item_id] = {
                "run": list(run_identity(row)),
                "stage": stage,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            }
    random.SystemRandom().shuffle(items)
    (out / "private").mkdir(parents=True, exist_ok=True)
    (out / "judge-views.jsonl").write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")
    (out / "private" / "judge-map.json").write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    return len(items)


def read_native(path: Path, mapping_path: Path) -> tuple[dict, dict]:
    """Missing, malformed and duplicate judgments supply no answer and never select an automatic judge."""
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        mapping = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    answers = {}
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        item_id = item["id"]
        answers[item_id] = None if item_id in answers else item
    return mapping if isinstance(mapping, dict) else {}, answers


def native_answers(case: Case, key: dict, row: dict, mapping: dict, judgments: dict) -> dict:
    answers = {}
    identity = list(run_identity(row))
    # Four-field native maps predate client modes and describe plugin runs only.
    compatible = (identity, identity[:-1]) if identity[-1] == DEFAULT_CLIENT_MODE else (identity,)
    for stage, (prompt, criteria) in native_views(case, key, row).items():
        # Bind an answer to the exact evidence and rubric; stale imports become unknown instead of scoring another run.
        matches = [item_id for item_id, item in mapping.items() if isinstance(item, dict)
                   and item.get("run") in compatible and item.get("stage") == stage
                   and item.get("prompt_sha256") == hashlib.sha256(prompt.encode()).hexdigest()]
        item = judgments.get(matches[0]) if len(matches) == 1 else None
        answer = judge.parse_answer(json.dumps(item), criteria)
        answers[stage] = answer if Draft202012Validator(judge.answer_schema(criteria)).is_valid(answer) else None
    return answers


def score_all(runs: Sequence[Path], *, out: Path, judge_model: str = "opus", judgments: Path | None = None, automatic_judge: bool = False, case_dir: Path | None = None) -> list[dict]:
    cases = {c.id: c for c in load_cases("all", case_dir=case_dir)}
    rows, _replaced = read_rows(runs, identity=run_identity)
    keys = refuse_unkeyed([cases[sid] for sid in dict.fromkeys(r["scenario"] for r in rows)])
    out.mkdir(parents=True, exist_ok=True)
    scratch = out / "judge-cwd"
    scratch.mkdir(exist_ok=True)
    executable = discover_agent_envelope().executable if automatic_judge else None
    mapping, imported = read_native(judgments, out / "private" / "judge-map.json") if judgments is not None else ({}, {})
    scored = []
    with (out / "scores.jsonl").open("w", encoding="utf-8") as sink:
        for row in rows:
            case = cases[row["scenario"]]
            key = keys[case.id]
            mode = row.get("client_mode", DEFAULT_CLIENT_MODE)
            capture_error = client_mode_error(mode, row)
            recall_error = (row.get("fresh") or {}).get("error")
            if mode == "tool-only" and row.get("recall_enabled"):
                recall_error = capture_error or client_mode_error(mode, row.get("fresh") or {})
            answers = native_answers(case, key, row, mapping, imported) if not automatic_judge else None
            result = score_row(case, key, row, judge_model=judge_model, executable=executable, scratch=scratch, answers=answers, automatic_judge=automatic_judge)
            entry = {
                k: row[k]
                for k in ("scenario", "repeat", "model", "arm", "error", "cost_usd", "warming_replies")
                if k in row
            } | {
                "client_mode": mode,
                "error": capture_error,
                "hook_invocations": row.get("hook_invocations"),
                "hook_starts": row.get("hook_starts"),
                "fresh_hook_invocations": (row.get("fresh") or {}).get("hook_invocations"),
                "fresh_hook_starts": (row.get("fresh") or {}).get("hook_starts"),
                "order": (row.get("observed") or {}).get("order"),
                "restraint_case": not key["must_change"],
                "recap_rule": key["recap"],
                "recap_produced": bool((row.get("observed") or {}).get("recaps")) if capture_error is None else None,
                "recall_enabled": row.get("recall_enabled", False),
                "recall_error": recall_error,
                "recall_cost_usd": (row.get("fresh") or {}).get("cost_usd"),
            } | result
            sink.write(json.dumps(entry) + "\n")
            sink.flush()
            scored.append(entry)
    shutil.rmtree(scratch, ignore_errors=True)
    return scored


def measures(row: dict) -> dict[str, str]:
    """Each measure keeps its own applicability and unknown state."""
    outcomes = {o["check"]: checks.Outcome(**o) for o in row["deterministic"] + row["judged"]}

    def joined(names: tuple[str, ...]) -> str:
        if row["error"] is not None or any(name not in outcomes for name in names):
            return checks.UNKNOWN
        return checks.verdict(None, [outcomes[name] for name in names], [])

    result = {
        "canonical correctness": checks.NOT_APPLICABLE if row["restraint_case"] else joined(("must_change", "J1", "J3")),
        "restraint": joined(("must_not_change", "new_pages", "J2")) if row["restraint_case"] else checks.NOT_APPLICABLE,
        "citation adherence": outcomes["recap_cited"].verdict if "recap_cited" in outcomes else checks.UNKNOWN,
        "fresh recall": row["recall"]["verdict"],
    }
    for rule in RECAP_RULES:
        result[f"recap production ({rule})"] = (
            checks.NOT_APPLICABLE if row["recap_rule"] != rule
            else checks.UNKNOWN if row["recap_produced"] is None
            else checks.PASS if row["recap_produced"] else checks.FAIL
        )
    return result


def build_report(scored: list[dict], *, baseline: str, candidate: str) -> str:
    """Separate denominators; average paired repetitions within each case before resampling cases."""
    arms = (baseline, candidate)
    selected = [row for row in scored if row["arm"] in arms]
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for row in selected:
        cells[(row.get("client_mode", DEFAULT_CLIENT_MODE), row["model"], row["scenario"], row["arm"])].append(row)
    lines = [
        "| client mode | model | case | arm | runs | pass | fail | unknown |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for (mode, model, case, arm), rows in sorted(cells.items()):
        verdicts = [row["verdict"] for row in rows]
        lines.append(
            f"| {mode} | {model} | {case} | {arm} | {len(rows)} | {verdicts.count(checks.PASS)} | "
            f"{verdicts.count(checks.FAIL)} | {verdicts.count(checks.UNKNOWN)} |"
        )
    lines += [
        "",
        "Each measure reports passes/known runs, unknown runs and inapplicable runs separately.",
        "Canonical correctness covers cases that owe an update; restraint covers cases that owe no update.",
        "For recap production, pass means a recap exists; production on a forbidden case is unwanted.",
        "Fresh recall includes every input-enabled probe, regardless of capture success.",
        "",
        "| client mode | measure | arm | pass/known | unknown | not applicable |",
        "|---|---|---|---|---|---|",
    ]
    measured = [(row, measures(row)) for row in selected]
    names = list(measures(selected[0])) if selected else []
    modes = sorted({row.get("client_mode", DEFAULT_CLIENT_MODE) for row in selected})
    for mode in modes:
        for name in names:
            for arm in arms:
                verdicts = [values[name] for row, values in measured if row["arm"] == arm and row.get("client_mode", DEFAULT_CLIENT_MODE) == mode]
                passed = verdicts.count(checks.PASS)
                known = passed + verdicts.count(checks.FAIL)
                lines.append(
                    f"| {mode} | {name} | {arm} | {passed}/{known} | {verdicts.count(checks.UNKNOWN)} | "
                    f"{verdicts.count(checks.NOT_APPLICABLE)} |"
                )
    lines += [
        "",
        "Differences are candidate minus baseline. Repeats and models are averaged within each case; the 95% bootstrap resamples cases.",
        "Intervals that include zero mean no change. Unknown pairs are excluded and counted.",
    ]
    for mode in modes:
        for name in names:
            clusters: dict[tuple, dict] = defaultdict(dict)
            for row, values in measured:
                if row.get("client_mode", DEFAULT_CLIENT_MODE) != mode:
                    continue
                clusters[(row["scenario"], row["model"], row["repeat"])][row["arm"]] = values[name]
            by_case: dict[str, list[tuple[float, float]]] = defaultdict(list)
            unknown = 0
            for (case, _model, _repeat), values in clusters.items():
                pair = (values.get(baseline), values.get(candidate))
                if pair == (checks.NOT_APPLICABLE, checks.NOT_APPLICABLE):
                    continue
                if any(value not in (checks.PASS, checks.FAIL) for value in pair):
                    unknown += 1
                else:
                    by_case[case].append((float(pair[0] == checks.PASS), float(pair[1] == checks.PASS)))
            pairs = [
                (sum(pair[0] for pair in values) / len(values), sum(pair[1] for pair in values) / len(values))
                for values in by_case.values()
            ]
            if not pairs:
                lines.append(f"\n{mode} {name}: no complete paired case; {unknown} pair(s) unknown.")
                continue
            low, high = paired_interval(pairs)
            mean = sum(c - b for b, c in pairs) / len(pairs)
            conclusion = "no change" if low <= 0 <= high else "difference excludes zero"
            lines.append(
                f"\n{mode} {name}: {len(pairs)} paired case(s), difference {mean:+.2f} "
                f"(95% interval {low:+.2f} to {high:+.2f}; {conclusion}); {unknown} pair(s) unknown."
            )
    return "\n".join(lines) + "\n"
