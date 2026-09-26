"""Language-bias twins for dense multilingual recall (``recall-multilingual-twins-v1``).

A twin is a query in German, Russian, Japanese or Estonian about a topic that
only an ENGLISH page of the golden fixture (``tests/fixtures``) covers, plus a
same-language POISON page that shares a word with the query and is about
something else. The encoder is expected to rank the English gold first; the
bar is that fusion does not let the poison's shared word lift it above the
gold (design §17.6.2: the gold outranks the poison in at least 3 of 4
languages and never loses more than one rank to it).

`recall_multilingual` carries one twin per language. This module adds the
poison pages for two more per language, rendered beside it under the same
folder, so every language has three. The rows live in
``tests/golden/queries_multilingual_twins.yaml`` and use the same logical keys
(``<lang>:<name>`` or ``fixture:<vault path>``).

Every name and place is invented.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from . import recall_multilingual

CORPUS_ID = "recall-multilingual-twins-v1"
QUERIES_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "golden" / "queries_multilingual_twins.yaml"
)

RecallPage = recall_multilingual.RecallPage

PAGES: tuple[RecallPage, ...] = (
    RecallPage(
        "de:urlaubsfreigabe",
        "Freigabe der Urlaubsanträge",
        "Die Freigabe der Urlaubsanträge erfolgt durch die Teamleitung. Für riskante "
        "Überschneidungen in der Ferienzeit gilt eine Sperrliste.",
    ),
    RecallPage(
        "de:hausprotokoll",
        "Protokoll der Hausversammlung",
        "Die Hausverwaltung schreibt das Protokoll der Hausversammlung zuerst als Entwurf. "
        "Danach wird es an alle Mieter verschickt.",
    ),
    RecallPage(
        "ru:vyklyuchatel",
        "Выключатель в коридоре",
        "Выключатель света в коридоре сломался. Электрик заменит выключатель в пятницу и "
        "проверит проводку для остальных этажей.",
    ),
    RecallPage(
        "ru:zhurnal",
        "Журнал посещений",
        "Журнал посещений спортзала ведёт администратор. Каждая запись в журнале содержит "
        "имя и время прихода.",
    ),
    RecallPage(
        "ja:switch",
        "照明のスイッチ交換",
        "廊下の照明のスイッチが壊れたので、来週の月曜日に電気工事の業者が新しいスイッチに交換する。",
    ),
    RecallPage(
        "ja:diary",
        "日誌の書き込み当番",
        "学級日誌の書き込み当番は出席番号順に回る。放課後までに担任の先生へ提出する。",
    ),
    RecallPage(
        "et:valguslyliti",
        "Valguslüliti vahetus",
        "Koridori valguslüliti läks katki. Elektrik vahetab lüliti reedel ja toob varuosad "
        "ka teise korruse jaoks.",
    ),
    RecallPage(
        "et:kylastuslogi",
        "Külastuste logi",
        "Spordisaali külastuste logi täidab administraator. Iga külastus kirjutatakse logisse "
        "koos nime ja kellaajaga.",
    ),
)

PAGES_BY_KEY: dict[str, RecallPage] = {page.key: page for page in PAGES}


def render(root: Path) -> dict[str, str]:
    """Write the poison pages under `root` (a vault root); return key -> vault path."""
    written: dict[str, str] = {}
    for page in PAGES:
        target = root / page.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"---\ntype: note\ntitle: {page.title}\nstatus: active\nupdated: 2026-09-01\n---\n"
            f"# {page.title}\n\n{page.body}\n",
            encoding="utf-8",
        )
        written[page.key] = page.rel_path
    return written


def resolve_key(key: str) -> str:
    """The canonical golden-set spelling of a logical key's page (see `recall_multilingual`)."""
    if key in PAGES_BY_KEY:
        path = PAGES_BY_KEY[key].rel_path
        return path.removesuffix(".md").removeprefix("Knowledge Base/").lower()
    return recall_multilingual.resolve_key(key)


def load_queries(path: Path = QUERIES_PATH) -> list[dict]:
    """The twin rows, validated against both page sets."""
    rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    for row in rows:
        if row["language"] not in recall_multilingual.LANGUAGES or row["kind"] != "language_bias":
            raise ValueError(f"not a twin row: {row!r}")
        for key in (*row["gold"], *row["poison"]):
            if key.startswith(recall_multilingual.FIXTURE_PREFIX):
                continue
            if key not in PAGES_BY_KEY and key not in recall_multilingual.PAGES_BY_KEY:
                raise ValueError(f"unknown logical key {key!r}")
    return rows


def corpus_digest() -> str:
    """Digest of the poison pages, so a silent content edit is visible in review."""
    payload = [[page.key, page.title, page.body] for page in PAGES]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
