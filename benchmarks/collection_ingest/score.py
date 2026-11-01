#!/usr/bin/env python
"""Score a finished drive run against the generator's answer key.

Reads three things: the answer key, the drive log, and the finished disposable vault.
The vault is observed through public behaviour only: page files (front matter
``imported_from`` and tags), and MCP reads (``read_memory`` links, ``search_memory``)
through a fresh server session started with the drive run's own isolated settings.

Every metric is a ``{numerator, denominator}`` pair. A pair that cannot be measured is
reported ``unknown`` with its reason, never as zero.

A key from a ``raw`` profile adds three sections. ``landing.lost`` counts key items that have
no page and no recorded refusal. ``raw`` scores each class of messy input: filed, refused or
lost, original bytes kept, and the probe phrase found by search. ``existing_vault`` scores a
run that started from pages the vault already held: none replaced, collisions renamed.

    python score.py --key K --drive-log D --out METRICS.json --python PY
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_session import McpSession, isolated_env  # noqa: E402

KB = "Knowledge Base"
# The baseline product lands every import here; a page elsewhere under Sources has a classified home.
UNCLASSIFIED_HOME = f"{KB}/Sources/Imported/"
RECALL_LIMIT = 5
SAMPLES = 5
FRONT_DELIM = "---"


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def frac(numerator: int, denominator: int) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator}


# --------------------------------------------------------------------------- #
# vault observation (files)
# --------------------------------------------------------------------------- #
def split_front_matter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Return ``(front matter, body)``; None when the file has no readable block."""
    if not text.startswith(FRONT_DELIM + "\n"):
        return None, text
    end = text.find("\n" + FRONT_DELIM, len(FRONT_DELIM) + 1)
    if end == -1:
        return None, text
    try:
        loaded = yaml.safe_load(text[len(FRONT_DELIM) + 1 : end])
    except yaml.YAMLError:
        return None, text
    return (loaded if isinstance(loaded, dict) else None), text[end + len(FRONT_DELIM) + 1 :]


def sha256_file(path: Path) -> str:
    """The sha256 of a file read in blocks: a stored original can be larger than memory allows."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def scan_vault(vault: Path) -> dict[str, Any]:
    pages: dict[str, dict[str, Any]] = {}
    media_files: dict[str, str] = {}
    unparseable = 0
    kb = vault / KB
    for path in sorted(kb.rglob("*")) if kb.is_dir() else []:
        if not path.is_file():
            continue
        rel = path.relative_to(vault).as_posix()
        if path.suffix.lower() == ".md":
            text = path.read_text(encoding="utf-8", errors="replace")
            front, body = split_front_matter(text)
            if front is None and text.startswith(FRONT_DELIM):
                unparseable += 1
            pages[rel] = {"front": front or {}, "text": text, "body": body}
        else:
            media_files[rel] = sha256_file(path)
    return {"pages": pages, "media_files": media_files, "unparseable_front_matter": unparseable}


def page_tags(front: dict[str, Any]) -> set[str]:
    raw = front.get("tags")
    items = raw if isinstance(raw, list) else str(raw or "").replace(",", " ").split()
    return {str(t).lstrip("#") for t in items}


def counterparts(scan: dict[str, Any], collection: str) -> dict[str, dict[str, list[str]]]:
    """Map original key path -> pages whose ``imported_from`` names it, by match form."""
    by_value: dict[str, list[str]] = {}
    for rel, page in scan["pages"].items():
        value = page["front"].get("imported_from")
        if isinstance(value, str):
            by_value.setdefault(value, []).append(rel)
    by_nfc: dict[str, list[str]] = {}
    for value, pages in by_value.items():
        by_nfc.setdefault(nfc(value), []).extend(pages)  # two names equal after NFC keep both pages
    return {"exact": by_value, "nfc": by_nfc, "prefix": collection}


def find_pages(index: dict[str, Any], key_path: str) -> tuple[list[str], str | None]:
    """Pages for an original path; the second value names how it matched."""
    prefix = index["prefix"]
    for form, value in (("exact", f"{prefix}/{key_path}"), ("exact-bare", key_path)):
        hit = index["exact"].get(value)
        if hit:
            return hit, form
    for form, value in (("nfc-only", f"{prefix}/{nfc(key_path)}"), ("nfc-only-bare", nfc(key_path))):
        hit = index["nfc"].get(value)
        if hit:
            return hit, form
    return [], None


# --------------------------------------------------------------------------- #
# link resolution against what a read returns
# --------------------------------------------------------------------------- #
class LinkTargets:
    """Decide whether an outbound entry from ``read_memory`` names a given page or file."""

    def __init__(self, scan: dict[str, Any]):
        stems = Counter(nfc(Path(p).stem) for p in scan["pages"])
        files = Counter(nfc(Path(p).name) for p in scan["media_files"])
        self.stems, self.files = stems, files

    def names_page(self, entry: str, page: str) -> str | None:
        # read_memory returns targets as written: with or without the ".md" suffix, never with a "#heading" or alias.
        entry_n = nfc(entry).removesuffix(".md")
        no_ext = nfc(page[:-3])
        for form, candidate in (("full-path", no_ext), ("kb-relative", no_ext.removeprefix(KB + "/"))):
            if entry_n == candidate:
                return form
        stem = nfc(Path(page).stem)
        if entry_n == stem and self.stems[stem] == 1:
            return "unique-stem"
        return None

    def names_file(self, entry: str, file: str) -> str | None:
        entry_n, file_n = nfc(entry), nfc(file)
        if entry_n in (file_n, file_n.removeprefix(KB + "/")):
            return "full-path"
        name = nfc(Path(file).name)
        if entry_n == name and self.files[name] == 1:
            return "unique-name"
        return None


def entries_of(data: Any) -> list[str]:
    links = data.get("links") if isinstance(data, dict) else None
    outbound = links.get("outbound") if isinstance(links, dict) else None
    out = []
    for item in outbound or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            out.append(str(item.get("path") or item.get("target") or item.get("to") or ""))
    return out


def result_paths(data: Any) -> list[str] | None:
    """Hit paths from a search reply, or None when the reply carries no hit list this scorer knows."""
    rows = data
    if isinstance(rows, dict):
        rows = rows.get("result", rows.get("results", rows))
    if isinstance(rows, dict):  # envelope form: {"hits": [...], plus advisory blocks}
        rows = rows.get("hits")
    if not isinstance(rows, list):
        return None
    return [str(row.get("path")) for row in rows if isinstance(row, dict) and row.get("path")]


@dataclass
class Probe:
    """One recall question: a phrase the key knows lives in a note or an attachment."""

    kind: str  # "note" or "media"
    lang: str
    query: str
    expected: set[str]  # pages that count as the item's counterpart; empty when it never landed
    strict: bool = False  # a counterpart page is in the top 5
    loose: bool = False  # the phrase is in the text of some page in the top 5
    first_strict: bool = False
    top5: list[str] = field(default_factory=list)
    item: str = ""  # the key path (raw) or page id (seeded) that the probe stands for
    promised: bool = False  # the import contract promises that search finds this phrase
    failed: bool = False  # the last search call failed or returned no hit list this scorer reads


def refusal_lookup(refusals: list[dict[str, Any]], collection: str) -> dict[str, str]:
    """Map each refused original path (NFC, with and without the collection prefix) to its code.
    A refusal that carries no code reads ``NO_CODE``: it is a refusal, but not a coded one."""
    out: dict[str, str] = {}
    for row in refusals:
        path = str(row["path"])
        for form in (path, path.removeprefix(collection + "/")):
            out[nfc(form)] = str(row.get("code") or NO_CODE)
    return out


def recall_table(probes: list[Probe], kind: str, flag: str, *, landed: bool = False) -> dict[str, Any]:
    slots: dict[str, list[int]] = {}
    for probe in probes:
        if probe.kind == kind and (probe.expected or not landed):
            slot = slots.setdefault(probe.lang, [0, 0])
            slot[0] += bool(getattr(probe, flag))
            slot[1] += 1
    return {lang: frac(*slot) for lang, slot in sorted(slots.items())}


# --------------------------------------------------------------------------- #
# raw items and the existing vault
# --------------------------------------------------------------------------- #
LOST_SAMPLES = 25
NO_CODE = "UNKNOWN"  # what a refusal reads when the reply names no code; drive.py uses the same label


def raw_pages(item: dict[str, Any], index: dict[str, Any], scan: dict[str, Any],
              twinned: set[str]) -> list[str]:
    """Pages for a raw item. Names that are equal after NFC (``twinned``) can share one
    ``imported_from`` or fall back to each other's page, so a twin counts only the pages that carry
    its own bytes: the page of a missing twin never stands in for it. Any other item keeps the
    pages found by name when no page names its bytes."""
    found, _ = find_pages(index, item["path"])
    same = [p for p in found if scan["pages"][p]["front"].get("original_sha256") == item["sha256"]]
    return same if nfc(item["path"]) in twinned else same or found


def original_bytes_held(item: dict[str, Any], pages: list[str], scan: dict[str, Any], vault: Path,
                        collection: str, inline_bound: int) -> str:
    """``artifact`` or ``text`` when the original bytes are held, ``missing`` when they are not,
    ``unknown`` when this scorer has no verified copy of the original to compare a text page with.

    Artifact bytes are found by hash among the stored files. A text original is found verbatim
    inside a page file: the vault's own placed copy of the original is the reference, and it is
    used only when its hash equals the key's.
    """
    if any(digest == item["sha256"] for digest in scan["media_files"].values()):
        return "artifact"
    if item["form"] == "artifact" or item["bytes"] > inline_bound:
        return "missing"
    placed = vault / collection / item["path"]
    if not placed.is_file() or sha256_file(placed) != item["sha256"]:
        return "unknown"
    original = placed.read_bytes()
    return "text" if any(original in (vault / page).read_bytes() for page in pages) else "missing"


def raw_section(key: dict[str, Any], raw: list[dict[str, Any]], pages_of: dict[str, list[str]],
                refused: dict[str, str], probes: list[Probe], scan: dict[str, Any], vault: Path,
                collection: str) -> dict[str, Any]:
    """Score every raw item: filed, refused or lost; bytes held; probe phrase found by search."""
    if not raw:
        return {"state": "unknown", "reason": "the key has no raw items (not a raw profile)"}
    inline_bound = key["raw"]["inline_text_bound"]
    probe_of = {p.item: p for p in probes if p.kind == "raw"}
    classes: dict[str, Counter[str]] = {}
    problems: list[dict[str, Any]] = []
    meets_by_item: dict[str, bool] = {}
    for item in raw:
        pages = pages_of[item["path"]]
        code = refused.get(nfc(item["path"]))
        state = "filed" if pages else ("refused" if code else "lost")
        # The key names no refusal code: any coded refusal meets a filed-or-refused item, and the code seen is reported.
        allowed = item["outcome"] == "filed-or-refused" and code != NO_CODE
        meets = state == "filed" or (state == "refused" and allowed)
        meets_by_item[item["path"]] = meets
        row = classes.setdefault(item["raw_class"], Counter())
        row["items"] += 1
        row[state] += 1
        if state == "refused":
            row["code:" + str(code)] += 1
        row["meets_key"] += meets
        if not meets:
            problems.append({"path": item["path"], "class": item["raw_class"], "state": state,
                             "code": code, "key_outcome": item["outcome"]})
        probe = probe_of.get(item["path"])
        if pages:
            held = original_bytes_held(item, pages, scan, vault, collection, inline_bound)
            row["bytes_" + ("held" if held in ("artifact", "text") else held)] += 1
            if held != "unknown":
                row["form_checked"] += 1
                row["form_as_key"] += held == item["form"]
            if probe is not None and probe.failed:
                row["probe_unknown"] += 1
            elif probe is not None:
                row["probed"] += 1
                row["found"] += probe.strict
                if probe.promised:
                    row["promised"] += 1
                    row["promised_found"] += probe.strict

    def table(row: Counter[str]) -> dict[str, Any]:
        checked = row["bytes_held"] + row["bytes_missing"]
        return {
            "items": row["items"],
            "filed": frac(row["filed"], row["items"]),
            "refused": row["refused"],
            "refused_by_code": {k.removeprefix("code:"): n for k, n in sorted(row.items())
                                if k.startswith("code:")},
            "lost": row["lost"],
            "meets_key": frac(row["meets_key"], row["items"]),
            "original_bytes_held_of_filed": {**frac(row["bytes_held"], checked),
                                             "unknown": row["bytes_unknown"]},
            "form_as_key_of_filed": {**frac(row["form_as_key"], row["form_checked"]),
                                     "unknown": row["bytes_unknown"]},
            "readable_observed_of_filed": {**frac(row["found"], row["probed"]),
                                           "unknown": row["probe_unknown"]},
            "readable_promised_of_filed": frac(row["promised_found"], row["promised"]),
        }

    total: Counter[str] = Counter()
    for row in classes.values():
        total.update(row)
    return {
        "state": "measured",
        "readable_means": "search_memory(probe phrase, limit 5) returns a page of the item's own "
                          "Source; 'promised' is the subset whose text the import contract keeps in a page",
        "all": table(total),
        "by_class": {name: {**table(row), "contract": key["raw"]["classes"][name]["contract"]}
                     for name, row in sorted(classes.items())},
        "not_meeting_key": problems[:LOST_SAMPLES],
        "not_meeting_key_count": len(problems),
        "meets_key_by_item": meets_by_item,
    }


def raw_gaps(section: dict[str, Any]) -> dict[str, int]:
    """Counts of raw checks that could not be made: each one makes the raw metrics a lower bound."""
    if section.get("state") != "measured":
        return {}
    row = section["all"]
    return {"probe_searches_failed": row["readable_observed_of_filed"]["unknown"],
            "originals_not_comparable": row["original_bytes_held_of_filed"]["unknown"]}


def first_journey_state(log: dict[str, Any]) -> str:
    """Whether the first journey ran to its end. A stopped run lists every item it never reached
    as lost, so the stop is named beside the count, and a log without a first journey is not a clean run."""
    journey = next((j for j in log["journeys"] if j["name"] == "first"), None)
    if journey is None:
        return "no first journey in the drive log"
    if "stopped_at" not in journey:
        return "the drive log does not record whether the first journey stopped"
    return f"stopped at {journey['stopped_at']}" if journey["stopped_at"] else "ran to the end"


def collided_name(page: str, stem: str) -> bool:
    """Whether ``page`` is named ``<stem>-<number>...``, the form the importer gives a taken name."""
    rest = Path(page).name.removeprefix(stem + "-")
    return rest != Path(page).name and rest.split(".")[0].isdigit()


def existing_vault_section(key: dict[str, Any], log: dict[str, Any], vault: Path,
                           pages_of: dict[str, list[str]], probes: list[Probe]) -> dict[str, Any]:
    """Whether the pages the vault held first survived, and how incoming items took their names."""
    block = key.get("existing_vault")
    seeded = log.get("existing_vault") or {}
    if not block:
        return {"state": "unknown", "reason": "the key has no existing_vault block (not a raw profile)"}
    if not seeded.get("seeded"):
        return {"state": "unknown",
                "reason": "the drive log records no seeded pages (the driver ran without --existing-pages)"}
    written = {row["id"]: row for row in seeded["pages"]}
    probe_of = {p.item: p for p in probes if p.kind == "seeded"}
    day = seeded["day"]
    unchanged: list[str] = []
    changed: list[str] = []
    missing: list[str] = []
    unknown: list[str] = []
    detail: list[dict[str, Any]] = []
    colliders: Counter[str] = Counter()
    bystanders: Counter[str] = Counter()
    for spec in block["pages"]:
        row = written.get(spec["id"])
        if row is None:
            unknown.append(spec["id"])
            continue
        file = vault / row["path"]
        if not file.is_file():
            missing.append(spec["id"])
        elif sha256_file(file) != row["sha256"]:
            changed.append(spec["id"])
        else:
            unchanged.append(spec["id"])
        if spec["role"] == "bystander":
            bystanders["items"] += 1
            bystanders["unchanged"] += spec["id"] in unchanged
            continue
        item_pages = pages_of.get(spec["collides_with"], [])
        renamed = bool(item_pages) and all(
            collided_name(p, spec["stem"].replace("{today}", day)) for p in item_pages)
        colliders["items"] += 1
        colliders["unchanged"] += spec["id"] in unchanged
        colliders["filed"] += bool(item_pages)
        colliders["renamed"] += renamed
        detail.append({"id": spec["id"], "item": spec["collides_with"], "seeded_page": row["path"],
                       "item_pages": item_pages, "renamed_by_the_allocator": renamed})
    asked = [p for p in probe_of.values() if not p.failed]
    return {
        "state": "partial" if unknown else "measured",
        "unknown_pages": unknown,
        "day": day,
        "seeded_pages_unchanged": frac(len(unchanged), len(block["pages"]) - len(unknown)),
        "seeded_pages_changed": changed,
        "seeded_pages_missing": missing,
        "colliding_items": {
            "item_filed": frac(colliders["filed"], colliders["items"]),
            "item_took_a_numbered_name": frac(colliders["renamed"], colliders["items"]),
            "seeded_page_unchanged": frac(colliders["unchanged"], colliders["items"]),
            "note": "a filed item that took no numbered name was planned on another day than the seed day",
        },
        "bystander_pages_unchanged": frac(bystanders["unchanged"], bystanders["items"]),
        "seeded_pages_found_by_search": {
            **frac(sum(p.strict for p in asked), len(asked)),
            "unknown": len(probe_of) - len(asked)},
        "colliders": detail,
    }


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
async def score(args: argparse.Namespace) -> dict[str, Any]:
    key = json.loads(args.key.read_text(encoding="utf-8"))
    log = json.loads(args.drive_log.read_text(encoding="utf-8"))
    vault, state = Path(log["paths"]["vault"]), Path(log["paths"]["state"])
    collection = log["paths"]["collection_name"]
    first_pages = set(log["snapshot_after_first_run"])
    scan = scan_vault(vault)
    index = counterparts(scan, collection)
    notes = [i for i in key["items"] if i["kind"] == "note"]
    atts = [i for i in key["items"] if i["kind"] == "attachment"]
    junk = [i for i in key["items"] if i["kind"] == "junk"]
    raw = [i for i in key["items"] if i["kind"] == "raw"]
    resolver = LinkTargets(scan)

    # ---- landing ---------------------------------------------------------
    match_forms: Counter[str] = Counter()
    pages_of: dict[str, list[str]] = {}
    for item in notes + atts + junk:
        pages, form = find_pages(index, item["path"])
        pages_of[item["path"]] = pages
        if form:
            match_forms[f"{item['kind']}:{form}"] += 1
    landed = {i["path"] for i in notes + atts if pages_of[i["path"]]}
    refusals = [r for j in log["journeys"] if j["name"] == "first" for r in j["refusals"]]
    refused_codes = refusal_lookup(refusals, collection)
    refused_by_key = {i["path"]: refused_codes[nfc(i["path"])]
                      for i in notes + atts if nfc(i["path"]) in refused_codes}
    absent = [i for i in notes + atts if i["path"] not in landed and i["path"] not in refused_by_key]
    nfc_names = Counter(nfc(i["path"]) for i in raw)
    twinned = {name for name, n in nfc_names.items() if n > 1}
    raw_pages_of = {i["path"]: raw_pages(i, index, scan, twinned) for i in raw}
    raw_lost = [i for i in raw if not raw_pages_of[i["path"]] and nfc(i["path"]) not in refused_codes]
    lost = [*absent, *raw_lost]

    def landing(items: list[dict[str, Any]]) -> dict[str, Any]:
        return frac(sum(1 for i in items if i["path"] in landed), len(items))

    attachments_by_ext = {
        ext: frac(sum(1 for i in atts if i["ext"] == ext and i["path"] in landed),
                  sum(1 for i in atts if i["ext"] == ext))
        for ext in sorted({i["ext"] for i in atts})
    }
    landing_metrics = {
        "notes_landed": landing(notes),
        "attachments_landed": landing(atts),
        "attachments_landed_by_extension": attachments_by_ext,
        "japanese_notes_landed": frac(sum(1 for i in notes if i["lang"] == "ja" and i["path"] in landed),
                                      sum(1 for i in notes if i["lang"] == "ja")),
        "nfd_named_notes_landed": frac(
            sum(1 for i in notes if i["nfd_filename"] and i["path"] in landed),
            sum(1 for i in notes if i["nfd_filename"])),
        "items_refused": frac(len(refused_by_key), len(notes) + len(atts)),
        "items_refused_by_code": dict(sorted(Counter(refused_by_key.values()).items())),
        "items_silently_absent": frac(len(absent), len(notes) + len(atts)),
        # An item with no page and no refusal recorded by the first journey.
        "lost": {**frac(len(lost), len(notes) + len(atts) + len(raw)),
                 "by_kind": dict(Counter(i["kind"] for i in lost)),
                 "paths": [i["path"] for i in lost[:LOST_SAMPLES]],
                 "first_journey": first_journey_state(log)},
        "items_silently_absent_by_kind": dict(Counter(i["kind"] for i in absent)),
        "items_silently_absent_by_extension": dict(
            Counter(i.get("ext", ".md") for i in absent).most_common()),
        "junk_landed": frac(sum(1 for i in junk if pages_of[i["path"]]), len(junk)),
        "junk_landed_by_kind": dict(Counter(i["junk_kind"] for i in junk if pages_of[i["path"]])),
        "imported_from_match_forms": dict(sorted(match_forms.items())),
        "unparseable_front_matter_pages": scan["unparseable_front_matter"],
    }

    # ---- classified homes, tags, properties ------------------------------
    # An item has a classified home when any of its pages sits outside the unclassified holding folder.
    homed = [(i, [p for p in pages_of[i["path"]] if not p.startswith(UNCLASSIFIED_HOME)])
             for i in notes + atts if pages_of[i["path"]]]
    outside = [p for _, pages in homed for p in pages]
    homes = {
        "classified_homes": frac(sum(1 for _, pages in homed if pages), len(homed)),
        "page_folders": dict(Counter(
            Path(p).parent.as_posix() for i in notes + atts for p in pages_of[i["path"]]).most_common(12)),
    }
    tag_hit = tag_all = tag_hit_landed = tag_all_landed = 0
    origin: dict[str, list[int]] = {"frontmatter": [0, 0], "inline": [0, 0]}
    prop_hit = prop_all = alias_hit = alias_all = 0
    for note in notes:
        pages = pages_of[note["path"]]
        have: set[str] = set().union(*(page_tags(scan["pages"][p]["front"]) for p in pages)) if pages else set()
        for tag in note["tags"]:
            tag_all += 1
            tag_hit += tag in have
            if pages:
                tag_all_landed += 1
                tag_hit_landed += tag in have
        for name, tags in (("frontmatter", note["frontmatter_tags"]), ("inline", note["inline_tags"])):
            origin[name][1] += len(tags)
            origin[name][0] += sum(1 for t in tags if t in have)
        fronts = [scan["pages"][p]["front"] for p in pages]
        for prop, value in note["properties"].items():
            prop_all += 1
            prop_hit += any(str(f.get(prop)) == str(value) for f in fronts)
        for alias in note["aliases"]:
            alias_all += 1
            alias_hit += any(alias in (f.get("aliases") or []) for f in fronts)
    fidelity = {
        "tags_carried": frac(tag_hit, tag_all),
        "tags_carried_of_landed_notes": frac(tag_hit_landed, tag_all_landed),
        "tags_carried_by_origin": {k: frac(*v) for k, v in origin.items()},
        "front_matter_properties_carried": frac(prop_hit, prop_all),
        "aliases_carried": frac(alias_hit, alias_all),
    }

    # ---- MCP reads: links, embeds, recall --------------------------------
    env = isolated_env(state=state, vault=vault, server_root=Path(log["server_root"]),
                       extra=log["env_extra"])
    link_total = link_kept = link_both_landed = link_both_kept = 0
    link_by_form: dict[str, list[int]] = {}
    embed_total = embed_kept = embed_both_landed = 0
    link_samples: list[dict[str, Any]] = []
    # What each key item and each distinct edge came to, for a caller that compares two runs item by item.
    edge_kept: dict[str, bool] = {}
    embed_edge_kept: dict[str, bool] = {}
    pairs_seen: set[tuple[str, str]] = set()
    entry_forms: Counter[str] = Counter()
    probes: list[Probe] = []
    settle = {"wait_s": args.settle_s, "requeried": 0, "recovered_note": 0, "recovered_media": 0}
    unreadable_replies = 0
    started = time.perf_counter()
    async with McpSession(python=args.python, state=state, env=env, timeout=args.timeout) as session:
        outbound_cache: dict[str, list[str]] = {}

        async def outbound(note_path: str) -> list[str]:
            if note_path not in outbound_cache:
                _, data, _ = await session.call("read_memory", {"path": note_path, "links": True},
                                                journey="score", step="links")
                outbound_cache[note_path] = entries_of(data)
            return outbound_cache[note_path]

        for note in notes:
            own = pages_of[note["path"]]
            resolvable = [lk for lk in note["links"] if lk["resolution"] == "resolvable"]
            if not (resolvable or note["embeds"]):
                continue
            entries: list[str] = []
            for page in own:
                entries += await outbound(page)
            for lk in resolvable:
                slot = link_by_form.setdefault(lk["form"], [0, 0])
                targets = pages_of.get(lk["target"], [])
                matched = [resolver.names_page(e, t) for e in entries for t in targets]
                kept = any(matched)
                if kept:
                    entry_forms[next(how for how in matched if how)] += 1
                slot[0] += kept
                slot[1] += 1
                pair = (note["path"], lk["target"])
                if pair in pairs_seen:
                    continue  # two links from one note to one target are one outbound edge
                pairs_seen.add(pair)
                both = bool(own and targets)
                edge_kept[f"{note['path']} -> {lk['target']}"] = bool(kept)
                link_total += 1
                link_kept += kept
                link_both_landed += both
                link_both_kept += kept and both
                if len(link_samples) < SAMPLES and (kept or both):
                    link_samples.append({"note": note["path"], "link": lk["raw"], "expected": lk["target"],
                                         "note_pages": own, "outbound": entries[:8], "kept": kept})
            for emb in note["embeds"]:
                if (note["path"], emb["attachment"]) in pairs_seen:
                    continue  # one outbound edge per (note, attachment)
                pairs_seen.add((note["path"], emb["attachment"]))
                att = next(a for a in atts if a["path"] == emb["attachment"])
                targets = pages_of[att["path"]]
                files = [f for f, digest in scan["media_files"].items() if digest == att["sha256"]]
                embed_total += 1
                embed_both_landed += bool(own and (targets or files))
                hit = any(resolver.names_page(e, t) for e in entries for t in targets) or any(
                    resolver.names_file(e, f) for e in entries for f in files)
                embed_kept += bool(hit and own)
                embed_edge_kept[f"{note['path']} -> {emb['attachment']}"] = bool(hit and own)

        # recall@5: one query per note probe and one per distinct media phrase
        for note in notes:
            probes.append(Probe("note", note["probe"]["lang"], note["probe"]["text"],
                                set(pages_of[note["path"]])))
        for item in raw:
            if item["probe"]:
                probes.append(Probe("raw", item["probe"]["lang"], item["probe"]["text"],
                                    set(raw_pages_of[item["path"]]), item=item["path"],
                                    promised=item["probe"]["promised"]))
        seeded_rows = {r["id"]: r for r in (log.get("existing_vault") or {}).get("pages", [])}
        for spec in (key.get("existing_vault") or {}).get("pages", []):
            if spec["id"] in seeded_rows:
                probes.append(Probe("seeded", "en", spec["probe"], {seeded_rows[spec["id"]]["path"]},
                                    item=spec["id"]))
        seen: set[str] = set()
        for att in atts:
            phrase = (att["media_phrase"] or {}).get("text")
            if not phrase or phrase in seen:
                continue
            seen.add(phrase)
            group = [a for a in atts if (a["media_phrase"] or {}).get("text") == phrase]
            expected = {p for a in group for p in pages_of[a["path"]]} | {
                f + ".md" for a in group for f, d in scan["media_files"].items()
                if d in {a["sha256"] for a in group}}
            probes.append(Probe("media", att["media_phrase"]["lang"], phrase, expected))

        async def ask(probe: Probe) -> None:
            outcome, data, _ = await session.call(
                "search_memory", {"query": probe.query, "limit": RECALL_LIMIT},
                journey="score", step=f"recall-{probe.kind}", retry_deadline_s=args.warm_deadline)
            nonlocal unreadable_replies
            paths = result_paths(data) if outcome == "ok" else []
            if paths is None:
                unreadable_replies += 1
            probe.failed = paths is None or outcome != "ok"
            probe.top5 = paths or []
            probe.strict = probe.strict or bool(probe.expected & set(probe.top5))
            needle = probe.query.lower()
            probe.loose = probe.loose or any(
                needle in scan["pages"].get(path, {"text": ""})["text"].lower() for path in probe.top5)

        for probe in probes:
            await ask(probe)
            probe.first_strict = probe.strict
        # A fresh server may still be catching up with the vault: ask every miss once more after a pause.
        missed = [p for p in probes if not p.strict]
        if missed and args.settle_s > 0:
            await asyncio.sleep(args.settle_s)
            for probe in missed:
                await ask(probe)
                settle["requeried"] += 1
                settle["recovered_" + probe.kind] = settle.get("recovered_" + probe.kind, 0) + probe.strict
    scorer_calls = session.calls
    scorer_wall = round(time.perf_counter() - started, 1)
    raw_metrics = raw_section(key, raw, raw_pages_of, refused_codes, probes, scan, vault, collection)
    raw_meets_key = raw_metrics.pop("meets_key_by_item", {})  # reported once, under per_item
    existing_metrics = existing_vault_section(key, log, vault, raw_pages_of, probes)
    ranked = [p for p in probes if p.kind in ("note", "media")]  # the samples below stay as they were

    # ---- media states ------------------------------------------------------
    media_status = (log["media"]["after_rerun"].get("final_status") or {})
    jobs = media_status.get("jobs") or []
    states: Counter[str] = Counter()
    per_attachment: list[dict[str, Any]] = []
    errors: Counter[str] = Counter()
    for att in atts:
        own = pages_of[att["path"]]
        files = [f for f, d in scan["media_files"].items() if d == att["sha256"]]
        wanted = set(own) | set(files) | {f + ".md" for f in files}
        job = next((j for j in jobs if j.get("path") in wanted or j.get("sidecar_path") in wanted), None)
        phrase = (att["media_phrase"] or {}).get("text")
        if not (own or files):
            state = "not-landed"
        elif job:
            state = str(job.get("state"))
            errors[str(job.get("error"))] += 1
        elif phrase and any(phrase.lower() in scan["pages"].get(p, {"text": ""})["text"].lower()
                            for p in wanted if p.endswith(".md")):
            state = "text-found-no-job"
        else:
            state = "unknown-no-job-no-text"
        states[state] += 1
        per_attachment.append({"path": att["path"], "ext": att["ext"], "state": state})
    media = {
        "states": dict(sorted(states.items())),
        "job_errors": dict(errors),
        "queue_converged_after_first_run": log["media"]["after_first_run"]["converged"],
        "queue_converged_after_rerun": log["media"]["after_rerun"]["converged"],
        "final_queue_counts": media_status.get("counts"),
        "per_attachment": per_attachment,
    }

    # ---- re-run duplicates ---------------------------------------------------
    # A Source names its original in `imported_from`; a draft note made from an own note
    # does not, and names its place in the collection in `collection_path` instead.
    rerun_front = [scan["pages"].get(p, {"front": {}})["front"]
                   for p in log["snapshot_after_rerun"] if p not in first_pages]
    rerun_sources = [f for f in rerun_front if f.get("imported_from")]
    rerun_notes = [f for f in rerun_front if f.get("collection_path") and not f.get("imported_from")]
    multiple = [i["path"] for i in notes + atts if len(pages_of[i["path"]]) > 1]
    duplicates = {
        "new_source_pages_from_rerun": len(rerun_sources),
        "new_note_pages_from_rerun": len(rerun_notes),
        "items_with_more_than_one_page": frac(len(multiple), len(landed)),
        "collection_label": log.get("collection_label"),
    }

    metrics = {
        "format": 1,
        "key": {"path": str(args.key), "profile": key["profile"], "seed": key["seed"],
                "fixture_tree_sha256": key["fixture_tree_sha256"], "summary": key["summary"]},
        "drive": {"path": str(args.drive_log), "profile": log["profile"], "server": log["server"],
                  "label": log.get("label")},
        "landing": landing_metrics,
        "raw": raw_metrics,
        "existing_vault": existing_metrics,
        "links_preserved": {
            "of_all_resolvable_links": frac(link_kept, link_total),
            "of_links_with_both_ends_landed": frac(link_both_kept, link_both_landed),
            "unit": "distinct (source note, target note) pairs",
            "occurrences_by_form": {k: frac(*v) for k, v in sorted(link_by_form.items())},
            "outbound_entry_forms_matched": dict(entry_forms),
        },
        "embeds_bound": {"of_all_embeds": frac(embed_kept, embed_total),
                         "of_embeds_with_both_ends_landed": frac(embed_kept, embed_both_landed)},
        "per_item": {
            "unit": "key path -> page count; edges are 'source -> target' over distinct pairs",
            "pages_per_item": {i["path"]: len(pages_of[i["path"]]) for i in notes + atts},
            "junk_pages": {i["path"]: len(pages_of[i["path"]]) for i in junk},
            "links_kept": edge_kept,
            "embeds_kept": embed_edge_kept,
            "raw_meets_key": raw_meets_key,
        },
        "fidelity": fidelity,
        "homes": homes,
        "rerun_duplicates": duplicates,
        "media": media,
        "recall_at_5": {
            "note_probes": {
                "counterpart_in_top5": recall_table(probes, "note", "strict"),
                "counterpart_in_top5_of_landed_notes": recall_table(probes, "note", "strict", landed=True),
                "counterpart_in_top5_first_pass": recall_table(probes, "note", "first_strict"),
                "phrase_on_any_top5_page": recall_table(probes, "note", "loose"),
            },
            "media_phrases": {
                "counterpart_in_top5": recall_table(probes, "media", "strict"),
                "counterpart_in_top5_of_landed_attachments": recall_table(
                    probes, "media", "strict", landed=True),
                "counterpart_in_top5_first_pass": recall_table(probes, "media", "first_strict"),
                "text_on_any_top5_page": recall_table(probes, "media", "loose"),
            },
            "settle_requery_of_misses": settle,
            "search": {"tool": "search_memory", "limit": RECALL_LIMIT, "mode": "default (hybrid)",
                       "effective": log["search_mode_note"], "scope": "default (kb)"},
        },
        "cost": {
            "drive_tool_calls": log["tool_calls_total"],
            "drive_tool_calls_by_tool": dict(Counter(c["tool"] for c in log.get("calls", []))),
            "drive_total_wall_s": log["total_wall_s"],
            "drive_phase_wall_ms": {j["name"]: j["phase_wall_ms"] for j in log["journeys"]},
            "drive_call_failures": log["call_failures"],
            "folder_kind_rules": log["folder_kind_rules"],
            "collection_label": log["collection_label"],
            "scorer_tool_calls": len(scorer_calls),
            "scorer_wall_s": scorer_wall,
            "scorer_call_failures": dict(Counter(
                f"{c['outcome']}:{c['error_code']}" for c in scorer_calls if c["outcome"] != "ok")),
        },
        # A read or search that failed counts as "not preserved / not found" above, so each one makes
        # the affected metric a lower bound. Zero here means every metric above was fully measured.
        "measurement_gaps": {
            **Counter(c["tool"] for c in scorer_calls if c["outcome"] != "ok"),
            **({"search_memory_unreadable_reply": unreadable_replies} if unreadable_replies else {}),
            **{f"raw_{name}": count for name, count in raw_gaps(raw_metrics).items() if count},
        },
        "samples": {
            "silently_absent": [i["path"] for i in absent[:SAMPLES]],
            "landed_notes": [{"key": i["path"], "pages": pages_of[i["path"]]}
                             for i in notes if pages_of[i["path"]]][:SAMPLES],
            "links": link_samples,
            "recall_misses_of_landed_items": [
                {"kind": p.kind, "query": p.query, "expected": sorted(p.expected), "top5": p.top5}
                for p in ranked if p.expected and not p.strict][:SAMPLES],
            "recall_hits": [{"kind": p.kind, "query": p.query, "top5": p.top5}
                            for p in ranked if p.strict][:SAMPLES],
            "classified_page_paths": outside[:SAMPLES],
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(metrics, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return metrics


def raw_headline(m: dict[str, Any]) -> dict[str, Any]:
    raw, vault = m["raw"], m["existing_vault"]
    out: dict[str, Any] = {}
    if raw["state"] == "measured":
        total = raw["all"]
        out["raw"] = {
            "meets_key": f"{total['meets_key']['numerator']}/{total['meets_key']['denominator']}",
            "filed": f"{total['filed']['numerator']}/{total['filed']['denominator']}",
            "refused": total["refused"], "lost": total["lost"],
            "readable_promised": (f"{total['readable_promised_of_filed']['numerator']}/"
                                  f"{total['readable_promised_of_filed']['denominator']}"),
            "classes_not_meeting_key": sorted(n for n, c in raw["by_class"].items()
                                              if c["meets_key"]["numerator"] != c["meets_key"]["denominator"]),
        }
    else:
        out["raw"] = raw
    if vault["state"] == "unknown":
        out["existing_vault"] = vault
    else:
        kept = vault["seeded_pages_unchanged"]
        out["existing_vault"] = {"state": vault["state"],
                                 "seeded_pages_unchanged": f"{kept['numerator']}/{kept['denominator']}",
                                 "changed": vault["seeded_pages_changed"],
                                 "missing": vault["seeded_pages_missing"]}
    return out


def headline(m: dict[str, Any]) -> dict[str, Any]:
    def pair(p: dict[str, Any]) -> str:
        return f"{p['numerator']}/{p['denominator']}"

    return {
        "measurement_gaps": m["measurement_gaps"],
        "notes_landed": pair(m["landing"]["notes_landed"]),
        "attachments_landed": pair(m["landing"]["attachments_landed"]),
        "refused": m["landing"]["items_refused_by_code"],
        "silently_absent": pair(m["landing"]["items_silently_absent"]),
        "lost": pair(m["landing"]["lost"]),
        "first_journey": m["landing"]["lost"]["first_journey"],
        "links_preserved": pair(m["links_preserved"]["of_all_resolvable_links"]),
        "embeds_bound": pair(m["embeds_bound"]["of_all_embeds"]),
        "tags_carried": pair(m["fidelity"]["tags_carried"]),
        "classified_homes": pair(m["homes"]["classified_homes"]),
        "rerun_new_source_pages": m["rerun_duplicates"]["new_source_pages_from_rerun"],
        "rerun_new_note_pages": m["rerun_duplicates"]["new_note_pages_from_rerun"],
        "media_states": m["media"]["states"],
        "recall_note_counterpart": {k: pair(v) for k, v in
                                    m["recall_at_5"]["note_probes"]["counterpart_in_top5"].items()},
        "recall_media_counterpart": {k: pair(v) for k, v in
                                     m["recall_at_5"]["media_phrases"]["counterpart_in_top5"].items()},
        "recall_media_text_any_page": {k: pair(v) for k, v in
                                       m["recall_at_5"]["media_phrases"]["text_on_any_top5_page"].items()},
        **raw_headline(m),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--drive-log", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="metrics JSON to write")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--warm-deadline", type=float, default=600.0,
                        help="seconds a search may wait for the index to finish warming")
    parser.add_argument("--settle-s", type=float, default=15.0,
                        help="pause before the one re-query of recall misses (0 disables it)")
    args = parser.parse_args(argv)
    metrics = asyncio.run(score(args))
    print(json.dumps(headline(metrics), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
