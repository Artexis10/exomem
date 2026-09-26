"""Multilingual recall pages for the lexical recall acceptance
(``recall-multilingual-v1``).

Sixty short pages, fifteen each in German, Russian, Japanese and Estonian,
rendered next to the English retrieval golden fixture (``tests/fixtures``).
The tree they make is used twice:

* as PADDING for the English non-regression arm: the golden queries run over
  the fixture plus these pages, so any drift in the English ranking caused by
  the non-English pages' tokens (corpus length, document frequency) shows up;
* as the MIXED-LANGUAGE VAULT for the multilingual rows in
  ``tests/golden/queries_multilingual.yaml``, whose golds name the logical keys
  below (``<lang>:<name>``) or a golden-fixture page (``fixture:<path>``).

Every name and place is invented. The pages are plain Markdown, written the
same way as the golden fixture they sit beside, so the lexical catalogue, the
in-process rung and the keyword lane all read them exactly as they read any
vault page.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

CORPUS_ID = "recall-multilingual-v1"
LANGUAGES: tuple[str, ...] = ("de", "ru", "ja", "et")
QUERY_KINDS: tuple[str, ...] = (
    "same_language",
    "cross_language",
    "morphology",
    "without_diacritics",
    "language_bias",
)
QUERIES_PATH = Path(__file__).resolve().parents[3] / "tests" / "golden" / "queries_multilingual.yaml"
FIXTURE_PREFIX = "fixture:"
_FOLDER = "Knowledge Base/Notes/Languages"


@dataclass(frozen=True)
class RecallPage:
    key: str
    title: str
    body: str

    @property
    def language(self) -> str:
        return self.key.split(":", 1)[0]

    @property
    def rel_path(self) -> str:
        return f"{_FOLDER}/{self.language}/{self.key.split(':', 1)[1]}.md"


def _pages(language: str, rows: tuple[tuple[str, str, str], ...]) -> tuple[RecallPage, ...]:
    return tuple(RecallPage(f"{language}:{name}", title, body) for name, title, body in rows)


PAGES: tuple[RecallPage, ...] = (
    *_pages(
        "de",
        (
            ("lieferplan", "Lieferplan der Firma Zölvarn",
             "Die Firma Zölvarn liefert die Ersatzteile für die Kühlanlage jeden Dienstag. "
             "Größere Lieferungen müssen zwei Wochen vorher bestätigt werden."),
            ("kuehlanlage", "Wartung der Kühlanlage",
             "Die Kühlanlage im Lager wird quartalsweise gewartet. Bei Störungen übernimmt "
             "der Techniker Jörven Brühl die Fehlersuche."),
            ("bruecke", "Prüfbericht Fußgängerbrücke",
             "Die Fußgängerbrücke über den Kanal zeigte Risse im Geländer. Die Prüfung ergab, "
             "dass die Schweißnähte erneuert werden müssen."),
            ("gaestebuch", "Gästebuch für das Sommerfest",
             "Das Gästebuch liegt am Eingang. Die Gäste tragen Namen und Grüße ein, und die "
             "Einträge werden später eingescannt."),
            ("schluessel", "Schlüsselübergabe im Büro",
             "Neue Mitarbeiter erhalten ihren Büroschlüssel am ersten Arbeitstag gegen "
             "Unterschrift. Verlorene Schlüssel müssen sofort gemeldet werden."),
            ("umbaukosten", "Größenordnung der Umbaukosten",
             "Die Größenordnung der Umbaukosten liegt bei achtzigtausend Euro. Die Schätzung "
             "stammt vom Architekturbüro Öhlhaus."),
            ("wiederholungspruefung", "Wiederholungsprüfung für Auszubildende",
             "Auszubildende, die die Zwischenprüfung nicht bestehen, dürfen sie nach einem "
             "Monat wiederholen. Die Wiederholung findet im Schulungsraum statt."),
            ("kantine", "Frühstück in der Kantine",
             "Die Kantine bietet montags Müsli und Brötchen an. Donnerstags gibt es Rührei."),
            ("parkplatz", "Parkplätze für Besucher",
             "Besucher parken auf den markierten Flächen vor dem Haupteingang."),
            ("druckerraum", "Drucker im zweiten Stock",
             "Der Drucker im zweiten Stock braucht neuen Toner. Papier liegt im Schrank."),
            ("betriebsausflug", "Betriebsausflug im Herbst",
             "Der Betriebsausflug führt dieses Jahr an den See. Die Anmeldung endet im August."),
            ("heizung", "Heizungsablesung",
             "Die Zählerstände der Heizung werden im Januar abgelesen und notiert."),
            ("fahrradkeller", "Fahrradkeller",
             "Fahrräder werden im Keller abgestellt. Der Schlüssel hängt beim Pförtner."),
            ("pflanzen", "Pflege der Büropflanzen",
             "Die Büropflanzen werden zweimal pro Woche gegossen, im Sommer öfter."),
            ("brandschutz", "Brandschutzübung",
             "Die jährliche Brandschutzübung beginnt mit dem Alarm um zehn Uhr. Alle "
             "sammeln sich am Treffpunkt auf dem Hof."),
        ),
    ),
    *_pages(
        "ru",
        (
            ("postavka", "Поставка деталей для склада",
             "Поставщик Велтран привозит детали для склада каждую среду. Крупные поставки "
             "согласуются за две недели."),
            ("krysha", "Ремонт крыши гаража",
             "Крыша гаража протекала после дождей. Бригада заменила кровельные листы и утеплитель."),
            ("biblioteka", "Каталог книг",
             "В библиотеке отдела хранятся книги по химии и физике. Новые книги вносятся в "
             "каталог по пятницам."),
            ("otpusk", "График отпусков",
             "График отпусков утверждается в марте. Сотрудники подают заявления через секретаря."),
            ("yolka", "Ёлка к празднику",
             "Ёлку для зала заказывают в декабре. Украшения хранятся в синих коробках."),
            ("toplivo", "Учёт топлива",
             "Водители записывают расход топлива в журнал. Перерасход проверяет механик."),
            ("povtornaya", "Повторная проверка знаний",
             "Сотрудники, не сдавшие экзамен, проходят повторную проверку через месяц."),
            ("kofe", "Кофемашина на кухне",
             "Кофемашину чистят по понедельникам. Зёрна покупают у местной обжарочной."),
            ("parkovka", "Парковка для гостей",
             "Гости оставляют машины на площадке у главного входа."),
            ("printer", "Принтер на третьем этаже",
             "В принтере на третьем этаже закончился тонер. Бумага лежит в шкафу."),
            ("pereezd", "Переезд отдела",
             "Отдел переезжает в новое здание в июле. Коробки выдаёт завхоз."),
            ("otoplenie", "Показания отопления",
             "Показания счётчиков отопления снимают в январе."),
            ("velosipedy", "Велосипеды во дворе",
             "Велосипеды ставят под навесом во дворе. Замок обязателен."),
            ("rasteniya", "Полив растений",
             "Растения в офисе поливают два раза в неделю."),
            ("trenirovka", "Учебная тревога",
             "Учебная тревога начинается в десять часов. Все собираются во дворе."),
        ),
    ),
    *_pages(
        "ja",
        (
            ("tower", "展望台の記録",
             "青葉タワーの高さは三百三十三メートルです。展望台は二つあります。"),
            ("minutes", "議事録の共有",
             "定例会議の議事録は翌日までに共有します。決定事項は赤字で記載します。"),
            ("inventory", "倉庫の在庫確認",
             "倉庫の在庫は毎週金曜日に確認します。不足している部品は発注リストに追加します。"),
            ("commute", "通勤経路の変更",
             "来月から通勤経路が変わります。快速電車は西浜駅に停車しません。"),
            ("garden", "庭の水やり",
             "夏の間、庭の水やりは朝と夕方の二回です。トマトの苗には肥料を与えます。"),
            ("backup", "サーバーのバックアップ",
             "サーバーのバックアップは毎晩二時に実行されます。復元の手順は手順書に記載されています。"),
            ("retest", "再試験の案内",
             "再試験は来月の第一月曜日に行います。再試験の会場は三階の研修室です。"),
            ("lunch", "社員食堂の献立",
             "社員食堂の献立は毎週月曜日に掲示されます。金曜日はカレーです。"),
            ("parking", "来客用の駐車場",
             "来客用の駐車場は正面玄関の前にあります。"),
            ("printer", "三階のプリンター",
             "三階のプリンターのトナーが切れています。用紙は棚にあります。"),
            ("moving", "部署の引っ越し",
             "部署は七月に新しい建物へ引っ越します。段ボールは総務で受け取れます。"),
            ("heating", "暖房の点検",
             "暖房設備の点検は十一月に行います。"),
            ("bicycle", "自転車置き場",
             "自転車は裏口の屋根の下に置いてください。"),
            ("plants", "観葉植物の世話",
             "事務所の観葉植物には週に二回水をあげます。"),
            ("drill", "避難訓練",
             "避難訓練は十時の警報で始まります。全員が中庭に集まります。"),
        ),
    ),
    *_pages(
        "et",
        (
            ("kylmkamber", "Külmkambri hooldus",
             "Laohoone külmkambrit kontrollitakse igal kuul. Rikete korral helistatakse "
             "valvetehnikule."),
            ("sookla", "Söökla nädalamenüü",
             "Söökla pakub esmaspäeval hernesuppi ja neljapäeval pannkooke."),
            ("votmed", "Võtmete üleandmine",
             "Uued töötajad saavad kontori võtmed esimesel tööpäeval. Kadunud võtmetest "
             "tuleb kohe teatada."),
            ("ulevaatus", "Sõidukite ülevaatus",
             "Ettevõtte sõidukite ülevaatus toimub igal kevadel. Järgmine ülevaatus on mais."),
            ("jatkamine", "Projekti jätkamine",
             "Projekti jätkamine sõltub rahastusest. Otsus tehakse pärast juhatuse koosolekut."),
            ("oppepaev", "Õppepäeva kava",
             "Õppepäev algab kell üheksa. Päeva lõpus toimub ühine arutelu."),
            ("korduseksam", "Korduseksami aeg",
             "Korduseksam toimub järgmise kuu esimesel esmaspäeval kolmanda korruse klassis."),
            ("parkla", "Külaliste parkla",
             "Külalised pargivad peasissepääsu ees oleval platsil."),
            ("printer", "Printer teisel korrusel",
             "Teise korruse printeril on toonerit vaja. Paber on kapis."),
            ("kolimine", "Osakonna kolimine",
             "Osakond kolib juulis uude majja. Kastid saab majandusjuhatajalt."),
            ("kute", "Küttenäidud",
             "Küttearvestite näidud loetakse jaanuaris üles."),
            ("jalgrattad", "Jalgrattaruum",
             "Jalgrattad pannakse keldrisse. Võti on valvelauas."),
            ("taimed", "Kontoritaimede kastmine",
             "Kontoritaimi kastetakse kaks korda nädalas."),
            ("oppus", "Evakuatsiooniõppus",
             "Evakuatsiooniõppus algab kell kümme häirega. Kõik kogunevad sisehoovi."),
            ("koosolek", "Juhatuse koosolek",
             "Juhatuse koosolek toimub neljapäeval. Päevakorras on eelarve ülevaade."),
        ),
    ),
)

PAGES_BY_KEY: dict[str, RecallPage] = {page.key: page for page in PAGES}


def render(root: Path) -> dict[str, str]:
    """Write every page under `root` (a vault root); return key -> vault path."""
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
    """The canonical golden-set spelling of a logical key's page.

    Matches ``scripts/eval_retrieval._canon``: no ``Knowledge Base/`` prefix, no
    ``.md`` suffix, lowercased.
    """
    if key.startswith(FIXTURE_PREFIX):
        path = key[len(FIXTURE_PREFIX):]
    else:
        path = PAGES_BY_KEY[key].rel_path
    path = path.removesuffix(".md").removeprefix("Knowledge Base/")
    return path.lower()


def load_queries(path: Path = QUERIES_PATH) -> list[dict]:
    """The multilingual golden rows, validated against this corpus."""
    rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    for row in rows:
        if row["language"] not in (*LANGUAGES, "en"):
            raise ValueError(f"unknown language in {row!r}")
        if row["kind"] not in QUERY_KINDS:
            raise ValueError(f"unknown kind in {row!r}")
        for key in (*row["gold"], *row.get("poison", ())):
            if not key.startswith(FIXTURE_PREFIX) and key not in PAGES_BY_KEY:
                raise ValueError(f"unknown logical key {key!r}")
    return rows


def corpus_digest() -> str:
    """Digest of the page set, so a silent content edit is visible in review."""
    payload = [[page.key, page.title, page.body] for page in PAGES]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
