#!/usr/bin/env python
"""Positive control for score.py: score a hand-built vault with known, planted losses.

The unmodified product loses nearly everything, so its near-zero metrics cannot show that
the scorer would notice a success. This script writes the collection into a disposable vault
the way an ideal adoption would, leaves out a counted set of pages, links, embeds and tags,
writes a drive log that says what a product would have reported, runs ``score.score`` and
compares each planted count with the metric the scorer printed. Any mismatch exits non-zero.

With a key from a ``raw`` profile the control also plants, among the raw items, a lost item
(no page, no refusal), a refused item, a page with the wrong bytes, a page whose phrase
search cannot find, an original the scorer cannot compare, an existing page that an import
overwrote and one that went missing, and a Unicode twin whose page is gone while the other
twin's page stays. The scorer must report each one by its count.

    python control.py --collection C --key K --workdir W --python PY --out METRICS.json
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import raw_corpus  # noqa: E402
import score  # noqa: E402
from drive import (  # noqa: E402
    REPO_ROOT,
    init_vault,
    kb_pages,
    place_collection,
    seed_existing_pages,
)
from mcp_session import isolated_env  # noqa: E402

IMPORTED = "Knowledge Base/Sources/Imported"
NOTES = "Knowledge Base/Notes/Insights"
CLASSIFIED = "Knowledge Base/Sources/Book/Media"
MISSING_NOTE, REFUSED_ATT, ABSENT_ATT = (17, 5), (4, 1), (5, 3)  # (modulus, remainder) by item index
DROPPED_LINK, DROPPED_EMBED, STRIPPED_NOTE, FILE_EMBED_ATT = 6, 5, (13, 2), 7
# Planted rules over the raw items, by their index in the key (sorted by path).
RAW_LOST, RAW_REFUSED, RAW_WRONG_BYTES, RAW_NO_PHRASE = (11, 3), (13, 5), (7, 2), (5, 1)
OVERWRITTEN_COLLIDER, DELETED_BYSTANDER = "collide-artifact", "bystander-source"
UNSAFE_NAME_REFUSAL = "NOT_FOUND"  # a code that an importer gives a name it cannot read; the key names none
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


def page_bytes(front: dict[str, Any], body: bytes) -> bytes:
    return f"---\n{yaml.safe_dump(front, allow_unicode=True, sort_keys=False)}---\n".encode() + body + b"\n"


def build_raw(key: dict[str, Any], collection: Path, vault: Path,
              existing: dict[str, Any]) -> dict[str, Any]:
    """Write a page (and a stored file) for each raw item that the plant rules leave; return the counts."""
    raw = [i for i in key["items"] if i["kind"] == "raw"]
    colliders = {s["collides_with"]: s for s in (key.get("existing_vault") or {}).get("pages", [])
                 if s["role"] == "collider"}
    seeded = {row["id"]: row for row in existing.get("pages", [])}
    day = existing.get("day", "")
    count: Counter[str] = Counter()
    lost_by_class: Counter[str] = Counter()
    codes: Counter[str] = Counter()
    refusals: list[dict[str, Any]] = []
    altered = False
    for n, item in enumerate(raw):
        path, cls, form = item["path"], item["raw_class"], item["form"]
        spec = colliders.get(path)
        refuse = UNSAFE_NAME_REFUSAL if item["outcome"] == "filed-or-refused" else None
        # The twin whose name is not in NFC loses its page; the other twin is kept, so the scorer
        # must not let the kept twin's page stand in for the lost one.
        gone_twin = cls == "unicode-twin" and unicodedata.normalize("NFC", path) != path
        kept_twin = cls == "unicode-twin" and not gone_twin
        if gone_twin or (spec is None and not kept_twin and hit(n, RAW_LOST)):
            lost_by_class[cls] += 1
            count["lost"] += 1
            continue
        if spec is None and not kept_twin and (refuse or hit(n, RAW_REFUSED)):
            code = refuse or "PLANTED_REFUSAL"
            refusals.append({"stage": "select.rejected", "path": f"{COLLECTION}/{path}", "code": code,
                             "reason": "planted"})
            count["refused"] += 1
            codes[code] += 1
            count["meets"] += bool(refuse)
            continue
        original = (collection / path).read_bytes()
        phrase = item["probe"]["text"].encode() if item["probe"] else None
        wrong = hit(n, RAW_WRONG_BYTES)
        if form == "text":
            body = ("§" + original.decode()[1:]).encode() if wrong else original
            if hit(n, RAW_NO_PHRASE) and phrase:
                body = body.replace(phrase, b"redacted")
            held = "unknown" if not altered and not wrong else ("held" if original in body else "missing")
            if held == "unknown":
                altered = True
                (vault / COLLECTION / path).write_bytes(original + b"\nAltered after the copy.\n")
        else:
            stored = vault / IMPORTED / f"raw-{n:04d}.bin"
            stored.write_bytes(b"planted wrong bytes" if wrong else original)
            held = "missing" if wrong else "held"
            body = b"stored as a file" + (b" " + phrase if phrase and n % 2 == 0 else b"")
        rel = f"{IMPORTED}/raw-{n:04d}.md"  # a flattened path can exceed the 255 byte name limit
        if spec is not None:
            rel = f"{IMPORTED}/{spec['stem'].replace('{today}', day)}-2.md"
            if spec["id"] == OVERWRITTEN_COLLIDER:
                rel = seeded[spec["id"]]["path"]  # the import replaced the page that was there
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_bytes(page_bytes(
            {"imported_from": f"{COLLECTION}/{path}", "original_sha256": item["sha256"],
             "original_bytes": item["bytes"], "tags": ["imported"]}, body))
        count["filed"] += 1
        count["meets"] += 1
        count["held"] += held == "held"
        count["missing"] += held == "missing"
        count["unknown"] += held == "unknown"
        count["form_as_key"] += held == "held"
        if phrase:
            count["probed"] += 1
            count["found"] += phrase in body
            if item["probe"]["promised"]:
                count["promised"] += 1
                count["promised_found"] += phrase in body
    seeded_gone = vault / seeded[DELETED_BYSTANDER]["path"]
    seeded_gone.unlink()
    return {"count": count, "total": len(raw), "lost_by_class": dict(lost_by_class), "codes": dict(codes),
            "refusals": refusals}


def build(key: dict[str, Any], collection: Path, vault: Path,
          existing: dict[str, Any] | None = None) -> dict[str, Any]:
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
    has_raw = any(i["kind"] == "raw" for i in key["items"])
    return {"raw": build_raw(key, collection, vault, existing or {}) if has_raw else None,
            "refused": refused, "jobs": jobs, "states": states, "expect": expect, "landed": len(page_of),
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
    existing = ({"seeded": False} if "existing_vault" not in key else
                seed_existing_pages(raw_corpus.vault_pages_path(args.key), vault, dt.date.today()))
    built = build(key, args.collection, vault, existing)
    first = kb_pages(vault)
    # A re-run that duplicated four landed notes: the scorer must see exactly four new pages.
    duplicated = [p for p in built["page_of"].values() if p.startswith(IMPORTED)][:4]
    for rel in duplicated:
        front, body = score.split_front_matter((vault / rel).read_text(encoding="utf-8"))
        write_page(vault, rel[:-3] + "-2.md", front or {}, body.strip())
    # And one draft note made twice from an own note: it names no `imported_from`.
    write_page(vault, f"{NOTES}/planted-rerun-note.md",
               {"type": "insight", "status": "draft", "import_collection": COLLECTION,
                "collection_path": built["notes"][0]["path"]}, "A planted re-run duplicate.")
    refusals = [{"stage": "select.rejected", "path": f"{COLLECTION}/{p}", "code": "PLANTED_REFUSAL",
                 "reason": "planted"} for p in built["refused"]]
    refusals += (built["raw"] or {}).get("refusals", [])
    log = {
        "format": 1, "profile": "control", "label": "positive control",
        "server": {"head": "control", "src_dirty": False}, "paths": {
            "vault": str(vault), "state": str(state), "collection_name": COLLECTION},
        "server_root": str(REPO_ROOT), "env_extra": {}, "search_mode_note": "control vault, lexical lane",
        "snapshot_after_first_run": first, "snapshot_after_rerun": kb_pages(vault),
        "existing_vault": existing,
        "tool_calls_total": 0, "total_wall_s": 0.0, "call_failures": {}, "folder_kind_rules": "n/a",
        "collection_label": "n/a",
        "journeys": [{"name": "first", "stopped_at": None, "refusals": refusals, "phase_wall_ms": {}},
                     {"name": "rerun", "refusals": [], "phase_wall_ms": {}}],
        "media": {"after_first_run": {"converged": True},
                  "after_rerun": {"converged": True, "final_status": {"counts": {}, "jobs": built["jobs"]}}},
    }
    log_path = workdir / "control.drive.json"
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    ns = argparse.Namespace(key=args.key, drive_log=log_path, out=args.out, python=args.python,
                            timeout=900.0, warm_deadline=600.0, settle_s=15.0, originals=None)
    metrics = asyncio.run(score.score(ns))
    return compare(metrics, built, key, len(duplicated), 1)


def pairs(metric: dict[str, int]) -> tuple[int, int]:
    return metric["numerator"], metric["denominator"]


def raw_checks(m: dict[str, Any], built: dict[str, Any], key: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """What the scorer must report for the raw items and the existing vault, from the planted counts."""
    if built["raw"] is None:
        return []
    c, total = built["raw"]["count"], built["raw"]["total"]
    r = m["raw"]["all"]
    absent_planted = (len(built["notes"]) + len([i for i in key["items"] if i["kind"] == "attachment"])
                      - built["notes_landed"] - built["atts_landed"] - len(built["refused"]))
    checks: list[tuple[str, Any, Any]] = [
        ("raw filed", pairs(r["filed"]), (c["filed"], total)),
        ("raw refused", r["refused"], c["refused"]),
        ("raw lost (planted loss caught)", r["lost"], c["lost"]),
        ("raw lost by class", {n: v["lost"] for n, v in m["raw"]["by_class"].items() if v["lost"]},
         built["raw"]["lost_by_class"]),
        ("lost, all kinds (landing.lost)", m["landing"]["lost"]["numerator"], absent_planted + c["lost"]),
        ("raw meets the key", pairs(r["meets_key"]), (c["meets"], total)),
        ("raw original bytes held", pairs(r["original_bytes_held_of_filed"]),
         (c["held"], c["held"] + c["missing"])),
        ("raw original not comparable (unknown)", r["original_bytes_held_of_filed"]["unknown"], c["unknown"]),
        ("raw gap reported for it", m["measurement_gaps"].get("raw_originals_not_comparable", 0), c["unknown"]),
        ("raw held in the form the key states", pairs(r["form_as_key_of_filed"]),
         (c["form_as_key"], c["filed"] - c["unknown"])),
        ("raw form not compared (unknown)", r["form_as_key_of_filed"]["unknown"], c["unknown"]),
        ("raw refusal reports the code seen", r["refused_by_code"],
         built["raw"]["codes"]),
        ("unicode twin with no page is lost, the kept twin is filed",
         (m["raw"]["by_class"]["unicode-twin"]["lost"], m["raw"]["by_class"]["unicode-twin"]["filed"]["numerator"]),
         (1, 1)),
        ("raw phrase found by search", pairs(r["readable_observed_of_filed"]),
         (c["found"], c["probed"])),
        ("raw phrase found, promised items", pairs(r["readable_promised_of_filed"]),
         (c["promised_found"], c["promised"])),
    ]
    vault = m["existing_vault"]
    # A run that never seeded pages, or a key without raw items, is unknown, not a perfect zero.
    checks += [
        ("existing vault not seeded is unknown", score.existing_vault_section(key, {}, Path(), {}, [])["state"],
         "unknown"),
        ("no raw items is unknown", score.raw_section({}, [], {}, {}, [], {}, Path(), "")["state"], "unknown"),
    ]
    if key.get("existing_vault"):
        pages = key["existing_vault"]["pages"]
        colliders = [p for p in pages if p["role"] == "collider"]
        bystanders = [p for p in pages if p["role"] == "bystander"]
        kept = [p for p in pages if p["id"] not in (OVERWRITTEN_COLLIDER, DELETED_BYSTANDER)]
        checks += [
            ("existing pages unchanged", pairs(vault["seeded_pages_unchanged"]), (len(kept), len(pages))),
            ("existing page overwritten", vault["seeded_pages_changed"], [OVERWRITTEN_COLLIDER]),
            ("existing page missing", vault["seeded_pages_missing"], [DELETED_BYSTANDER]),
            ("colliding items filed", pairs(vault["colliding_items"]["item_filed"]),
             (len(colliders), len(colliders))),
            ("colliding items renamed", pairs(vault["colliding_items"]["item_took_a_numbered_name"]),
             (len(colliders) - 1, len(colliders))),
            ("collider pages unchanged", pairs(vault["colliding_items"]["seeded_page_unchanged"]),
             (len(colliders) - 1, len(colliders))),
            ("bystander pages unchanged", pairs(vault["bystander_pages_unchanged"]),
             (len(bystanders) - 1, len(bystanders))),
            ("existing pages found by search", pairs(vault["seeded_pages_found_by_search"]),
             (len(kept), len(pages))),  # a kept page is read back by its phrase; the other two are not
        ]
    return checks


def compare(m: dict[str, Any], built: dict[str, Any], key: dict[str, Any], dup: int,
            dup_notes: int) -> int:
    e = built["expect"]
    notes, atts = built["notes"], [i for i in key["items"] if i["kind"] == "attachment"]
    checks = [
        ("notes landed", pairs(m["landing"]["notes_landed"]), (built["notes_landed"], len(notes))),
        ("attachments landed", pairs(m["landing"]["attachments_landed"]), (built["atts_landed"], len(atts))),
        ("first journey ran to the end", m["landing"]["lost"]["first_journey"], "ran to the end"),
        ("a stopped first journey names its stop",
         score.first_journey_state({"journeys": [{"name": "first", "stopped_at": "plan"}]}), "stopped at plan"),
        ("a log without a first journey is not a clean run",
         score.first_journey_state({"journeys": []}), "no first journey in the drive log"),
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
        ("re-run duplicate notes", m["rerun_duplicates"]["new_note_pages_from_rerun"], dup_notes),
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
    checks += raw_checks(m, built, key)
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
