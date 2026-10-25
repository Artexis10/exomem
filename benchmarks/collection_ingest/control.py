#!/usr/bin/env python
"""Positive control for score.py: score a hand-built vault with known, planted losses.

The unmodified product loses nearly everything, so its near-zero metrics cannot show that
the scorer would notice a success. This script writes the collection into a disposable vault
the way an ideal adoption would, leaves out a counted set of pages, links, embeds and tags,
writes a drive log that says what a product would have reported, runs ``score.score`` and
compares each planted count with the metric the scorer printed. Any mismatch exits non-zero.

    python control.py --collection C --key K --workdir W --python PY --out METRICS.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import score  # noqa: E402
from drive import REPO_ROOT, init_vault, kb_pages, place_collection  # noqa: E402
from mcp_session import isolated_env  # noqa: E402

IMPORTED = "Knowledge Base/Sources/Imported"
CLASSIFIED = "Knowledge Base/Sources/Book/Media"
MISSING_NOTE, REFUSED_ATT, ABSENT_ATT = (17, 5), (4, 1), (5, 3)  # (modulus, remainder) by item index
DROPPED_LINK, DROPPED_EMBED, STRIPPED_NOTE, FILE_EMBED_ATT = 6, 5, (13, 2), 7
JOB_STATES = ("blocked", "failed", "running")
COLLECTION = "Collection"


def flat(path: str) -> str:
    return path.replace("/", " - ")


def hit(index: int, rule: tuple[int, int]) -> bool:
    return index % rule[0] == rule[1]


def write_page(vault: Path, rel: str, front: dict[str, Any], body: str) -> None:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    head = yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
    target.write_text(f"---\n{head}---\n{body}\n", encoding="utf-8")


def build(key: dict[str, Any], collection: Path, vault: Path) -> dict[str, Any]:
    """Write the ideal-with-losses vault; return what the scorer should report."""
    items = [i for i in key["items"] if i["kind"] != "junk"]
    notes = [i for i in items if i["kind"] == "note"]
    atts = [i for i in items if i["kind"] == "attachment"]
    page_of: dict[str, str] = {}
    refused: list[str] = []
    for n, note in enumerate(notes):
        if not hit(n, MISSING_NOTE):
            home = CLASSIFIED if n % 3 == 0 else IMPORTED
            page_of[note["path"]] = f"{home}/{flat(note['path'][:-3])}.md"
    for n, att in enumerate(atts):
        if hit(n, REFUSED_ATT):
            refused.append(att["path"])
        elif not hit(n, ABSENT_ATT):
            page_of[att["path"]] = f"{IMPORTED}/{flat(att['path'])}.md"
    file_att = {a["path"] for n, a in enumerate(atts) if n % FILE_EMBED_ATT == 0 and a["path"] in page_of}
    att_by_path = {a["path"]: a for a in atts}
    # Byte-identical attachments share one stored file, so an embed of a twin that has no page still binds.
    file_by_sha = {a["sha256"]: Path(a["path"]).name for a in atts if a["path"] in file_att}

    def ref(path: str, style: int) -> str:
        page = page_of[path][:-3]
        short = page.removeprefix("Knowledge Base/")
        return [page, short, Path(page).name, short + ".md"][style % 4]

    expect: dict[str, Any] = dict.fromkeys(
        ["links_both", "links_kept", "embeds_both", "embeds_kept", "tags", "tags_kept", "aliases", "aliases_kept",
         "props", "props_kept", "classified", "recall_en", "recall_ja"], 0)
    link_n = embed_n = 0
    pair_kept: dict[tuple[str, str], bool] = {}  # two links from a note to one target are one outbound edge
    pair_both: dict[tuple[str, str], bool] = {}
    embed_kept: dict[tuple[str, str], bool] = {}
    embed_both: dict[tuple[str, str], bool] = {}
    for n, note in enumerate(notes):
        page = page_of.get(note["path"])
        stripped = hit(n, STRIPPED_NOTE)
        expect["tags"] += len(note["tags"])
        expect["aliases"] += len(note["aliases"])
        expect["props"] += len(note["properties"])
        lines = [f"# {note['title']}", "", note["probe"]["text"], ""]
        for link in (lk for lk in note["links"] if lk["resolution"] == "resolvable"):
            link_n += 1
            both = bool(page and link["target"] in page_of)
            kept = both and link_n % DROPPED_LINK != 0
            pair = (note["path"], link["target"])
            pair_kept[pair] = pair_kept.get(pair, False) or kept
            pair_both[pair] = both
            if kept:
                lines.append(f"- [[{ref(link['target'], link_n)}]]" if link_n % 3 else
                             f"- [[{ref(link['target'], link_n)}|alias {link_n}]]")
        for emb in note["embeds"]:
            embed_n += 1
            target = emb["attachment"]
            twin = file_by_sha.get(att_by_path[target]["sha256"])
            both = bool(page and (target in page_of or twin))
            kept = both and embed_n % DROPPED_EMBED != 0
            pair = (note["path"], target)
            embed_kept[pair] = embed_kept.get(pair, False) or kept
            embed_both[pair] = both
            if kept:
                if target in file_att or (target not in page_of and twin):
                    entry = f"Sources/Imported/files/{twin if target not in page_of else Path(target).name}"
                else:
                    entry = ref(target, 1)
                lines.append(f"![[{entry}]]")
        if page is None:
            continue
        front: dict[str, Any] = {"imported_from": f"{COLLECTION}/{note['path']}"}
        if not stripped:
            front = {"tags": note["tags"], "aliases": note["aliases"], **note["properties"], **front}
            expect["tags_kept"] += len(note["tags"])
            expect["aliases_kept"] += len(note["aliases"])
            expect["props_kept"] += len(note["properties"])
        else:
            front = {"tags": ["imported"], **front}
        expect["classified"] += page.startswith(CLASSIFIED)
        expect["recall_" + note["lang"]] += 1
        write_page(vault, page, front, "\n".join(lines))
    expect["links_both"] = sum(pair_both.values())
    expect["links_kept"] = sum(pair_kept.values())
    expect["embeds_both"] = sum(embed_both.values())
    expect["embeds_kept"] = sum(embed_kept.values())
    states: dict[str, int] = {}
    jobs: list[dict[str, Any]] = []
    for n, att in enumerate(atts):
        page = page_of.get(att["path"])
        if page is None:
            twin_only = "unknown-no-job-no-text" if att["sha256"] in file_by_sha else "not-landed"
            states[twin_only] = states.get(twin_only, 0) + 1
            continue
        phrase = (att["media_phrase"] or {}).get("text")
        write_page(vault, page, {"imported_from": f"{COLLECTION}/{att['path']}", "tags": ["imported"]},
                   phrase or att["path"])
        if att["path"] in file_att:
            copy = vault / "Knowledge Base/Sources/Imported/files" / Path(att["path"]).name
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes((collection / att["path"]).read_bytes())
        if n % 3 == 0:
            state = JOB_STATES[(n // 3) % len(JOB_STATES)]
            jobs.append({"path": page, "state": state, "error": f"planted {state}"})
        else:
            state = "text-found-no-job" if phrase else "unknown-no-job-no-text"
        states[state] = states.get(state, 0) + 1
    phrases = {(a["media_phrase"] or {}).get("text") for a in atts} - {None}
    landed_phrases = {(a["media_phrase"] or {}).get("text") for a in atts if a["path"] in page_of} - {None}
    expect["media_phrases"] = (len(landed_phrases), len(phrases))
    return {"refused": refused, "jobs": jobs, "states": states, "expect": expect, "landed": len(page_of),
            "notes_landed": sum(1 for n in notes if n["path"] in page_of),
            "atts_landed": sum(1 for a in atts if a["path"] in page_of),
            "notes": notes, "page_of": page_of}


def run(args: argparse.Namespace) -> int:
    key = json.loads(args.key.read_text(encoding="utf-8"))
    workdir = args.workdir.resolve()
    vault, state = workdir / "vault", workdir / "state"
    for folder in (vault, state):
        folder.mkdir(parents=True, exist_ok=True)
    env = isolated_env(state=state, vault=vault, server_root=REPO_ROOT)
    place_collection(args.collection, vault / COLLECTION)
    init = init_vault(args.python, env, vault, state)
    if init["exit_code"] != 0:
        raise SystemExit(f"exomem init failed: {init}")
    built = build(key, args.collection, vault)
    first = kb_pages(vault)
    # A re-run that duplicated four landed notes: the scorer must see exactly four new pages.
    duplicated = [p for p in built["page_of"].values() if p.startswith(IMPORTED)][:4]
    for rel in duplicated:
        front, body = score.split_front_matter((vault / rel).read_text(encoding="utf-8"))
        write_page(vault, rel[:-3] + "-2.md", front or {}, body.strip())
    refusals = [{"stage": "select.rejected", "path": f"{COLLECTION}/{p}", "code": "PLANTED_REFUSAL",
                 "reason": "planted"} for p in built["refused"]]
    log = {
        "format": 1, "profile": "control", "label": "positive control",
        "server": {"head": "control", "src_dirty": False}, "paths": {
            "vault": str(vault), "state": str(state), "collection_name": COLLECTION},
        "server_root": str(REPO_ROOT), "env_extra": {}, "search_mode_note": "control vault, lexical lane",
        "snapshot_after_first_run": first, "snapshot_after_rerun": kb_pages(vault),
        "tool_calls_total": 0, "total_wall_s": 0.0, "call_failures": {}, "folder_kind_rules": "n/a",
        "collection_label": "n/a",
        "journeys": [{"name": "first", "refusals": refusals, "phase_wall_ms": {}},
                     {"name": "rerun", "refusals": [], "phase_wall_ms": {}}],
        "media": {"after_first_run": {"converged": True},
                  "after_rerun": {"converged": True, "final_status": {"counts": {}, "jobs": built["jobs"]}}},
    }
    log_path = workdir / "control.drive.json"
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    ns = argparse.Namespace(key=args.key, drive_log=log_path, out=args.out, python=args.python,
                            timeout=900.0, warm_deadline=600.0, settle_s=15.0)
    metrics = asyncio.run(score.score(ns))
    return compare(metrics, built, key, len(duplicated))


def pairs(metric: dict[str, int]) -> tuple[int, int]:
    return metric["numerator"], metric["denominator"]


def compare(m: dict[str, Any], built: dict[str, Any], key: dict[str, Any], dup: int) -> int:
    e = built["expect"]
    notes, atts = built["notes"], [i for i in key["items"] if i["kind"] == "attachment"]
    checks = [
        ("notes landed", pairs(m["landing"]["notes_landed"]), (built["notes_landed"], len(notes))),
        ("attachments landed", pairs(m["landing"]["attachments_landed"]), (built["atts_landed"], len(atts))),
        ("refused by code", m["landing"]["items_refused_by_code"], {"PLANTED_REFUSAL": len(built["refused"])}),
        ("silently absent", m["landing"]["items_silently_absent"]["numerator"],
         len(notes) + len(atts) - built["notes_landed"] - built["atts_landed"] - len(built["refused"])),
        ("links preserved", pairs(m["links_preserved"]["of_links_with_both_ends_landed"]),
         (e["links_kept"], e["links_both"])),
        ("embeds bound", pairs(m["embeds_bound"]["of_embeds_with_both_ends_landed"]),
         (e["embeds_kept"], e["embeds_both"])),
        ("tags carried", pairs(m["fidelity"]["tags_carried"]), (e["tags_kept"], e["tags"])),
        ("aliases carried", pairs(m["fidelity"]["aliases_carried"]), (e["aliases_kept"], e["aliases"])),
        ("properties carried", pairs(m["fidelity"]["front_matter_properties_carried"]),
         (e["props_kept"], e["props"])),
        ("classified homes", m["homes"]["classified_homes"]["numerator"], e["classified"]),
        ("re-run duplicate pages", m["rerun_duplicates"]["new_source_pages_from_rerun"], dup),
        ("media states", m["media"]["states"], dict(sorted(built["states"].items()))),
        ("media phrases (counterpart in top 5)",
         tuple(sum(m["recall_at_5"]["media_phrases"]["counterpart_in_top5"][k][f] for k in
                   m["recall_at_5"]["media_phrases"]["counterpart_in_top5"])
               for f in ("numerator", "denominator")), e["media_phrases"]),
        ("recall en (counterpart in top 5)",
         m["recall_at_5"]["note_probes"]["counterpart_in_top5"]["en"]["numerator"], e["recall_en"]),
        ("recall ja (counterpart in top 5)",
         m["recall_at_5"]["note_probes"]["counterpart_in_top5"]["ja"]["numerator"], e["recall_ja"]),
    ]
    bad = 0
    for label, got, want in checks:
        status = "ok  " if got == want else "FAIL"
        bad += got != want
        print(f"{status} {label}: scorer={got} planted={want}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True, help="empty scratch directory")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--out", type=Path, required=True, help="metrics JSON the scorer writes")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
