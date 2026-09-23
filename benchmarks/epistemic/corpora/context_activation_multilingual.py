"""The multilingual sibling of the context-activation fixtures
(``context-activation-multilingual-v1``): English control turns plus German,
Russian, Japanese and Estonian turns against an English-majority vault that
also holds native-script anchors and pages.

It is a SIBLING, never an extension. The English set
(:mod:`epistemic.corpora.context_activation`) is digest-pinned at nine cases
and nine twins and stays byte-identical; this set has its own case ids, its
own digest and its own corpus. It reuses that module's product writers, so
every page here goes through the same canonical note, entity, resource,
Records and Planning writers.

Each row of the step-4 design table is one case (ids ``M<n>-<lang>``) or its
same-language negative twin (``N<n>-<lang>``):

* ``M1``/``N1`` named-rare: an English-authored anchor's invented name inside
  native prose; the twin names ANOTHER anchor while talking about the first.
* ``M2``/``N2`` cross-language menu: no name, the turn is about a page that
  sits in the 24-page recent pool below the 8-entry recency menu; the twin
  keeps the sentence frame and changes the topic.
* ``M3`` content-free: "let's continue", in each language.
* ``M4`` language bias: a Russian turn about an English page, with unrelated
  Russian pages (one of them an anchor) in the same pool.
* ``M5``/``N5`` CJK named: a Japanese anchor's name inside a Japanese
  sentence; the twin holds the name only inside an unrelated compound.
* ``M6`` continuity switch: a turn that names an anchor, then a same-language
  turn on another subject.
* ``M7`` carry fragment: one accented word that the ASCII catalogue splits.
* ``M8`` minority function word: a residual row, reported rather than scored.

Gold, poison and ``never_resolved`` name LOGICAL KEYS, never vault paths:
:func:`build_corpus` renders the corpus and returns the key -> path map, and
the explicit recency schedule a test supplies to the compiler
(``working_set._recent_mtimes``). Every name is invented; nothing here comes
from a real vault.

Why a case type of its own rather than the English set's ``FixtureCase``:
that type has one expected status, no language, no prior turn and a required
English-style reminder turn nothing in this step reads. The two arms
(semantic evidence on and off) expect different statuses for the named
cases, so both are recorded here. The expectations are step 4's targets: the
fragment correction and CJK containment are lexical, so they move the
semantic-off arm too (M5 and N5 are ``partial`` there, M7 never carries).

The German minority is six prose notes plus terse German anchor pages, so the
common German function words are common by document frequency here as they
are in any vault with more than a handful of German pages. With fewer, every
German turn pairs `der`/`ist`/`für` into a phrase and the carry names
unrelated German pages; that residual belongs to M8 alone, which uses `zur`,
a function word this corpus keeps on one page.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

from epistemic.corpora.context_activation import (
    EXPECTED_STATUSES,
    FixtureError,
    _compiled_note,
    _corpus_hash,
    _create_planning_item,
    _create_records_collection,
    _entity,
    _governed_resource,
    _planning_manifest,
    _records_manifest,
    find_normalized_leaks,
    find_verbatim_leaks,
)

FIXTURE_SET_ID = "context-activation-multilingual-v1"
CORPUS_ID = "context-activation-multilingual-corpus-v1"

LANGUAGES: tuple[str, ...] = ("en", "de", "ru", "ja", "et")

#: The design table's kinds. A positive kind needs a same-language twin of the
#: kind that maps back to it in :data:`TWIN_OF`.
KINDS: tuple[str, ...] = (
    "named_rare",
    "named_rare_twin",
    "cross_language_menu",
    "cross_language_menu_twin",
    "content_free",
    "language_bias",
    "cjk_named",
    "cjk_compound_twin",
    "continuity_switch",
    "carry_fragment",
    "minority_function_word",
)
POSITIVE_KINDS: frozenset[str] = frozenset({"named_rare", "cross_language_menu", "cjk_named"})
TWIN_OF: dict[str, str] = {
    "named_rare_twin": "named_rare",
    "cross_language_menu_twin": "cross_language_menu",
    "cjk_compound_twin": "cjk_named",
}

#: What the `recent_context` block must do for the case with semantic evidence
#: on: ``gold_first`` -- the gold leads the block; ``recency`` -- the block is
#: exactly the recency block; ``any`` -- no order constraint beyond poison.
MENU_EXPECTATIONS: tuple[str, ...] = ("gold_first", "recency", "any")

#: The recent pool similarity may reorder, and the recency menu it is cut to
#: (`working_set.RECENT_CONTEXT_MAX_ENTRIES`). Menu golds and poisons sit in
#: the pool BELOW the menu, so only relevance can bring them to the top.
RECENT_POOL_SIZE = 24
RECENT_MENU_SIZE = 8

#: The newest fixture page's mtime, and the gap between consecutive pages: an
#: hour, far outside the write-burst window, so no two pages read as a batch.
RECENCY_BASE_NS = int(datetime(2026, 9, 20, 12, 0, tzinfo=UTC).timestamp()) * 1_000_000_000
RECENCY_SPACING_NS = 3600 * 1_000_000_000


@dataclass(frozen=True)
class MultilingualCase:
    """One authored multilingual case or twin. Editing any field moves the digest.

    ``poison``: never resolved, carried, banded or promoted. ``never_resolved``:
    may surface as ``partial``, never ``resolved`` (the two-kinds-on-two-anchors
    and compound twins, and the anchor a topic switch leaves behind).
    ``rare_name``: the invented name or word the case turns on, where one does.
    ``scored``: False only for the residual row, which is reported.
    """

    case_id: str
    pairs_with: str | None
    language: str
    kind: str
    turn: str
    gold: tuple[str, ...]
    poison: tuple[str, ...]
    never_resolved: tuple[str, ...]
    expected_status_on: str
    expected_status_off: str
    menu: str
    risk: int | None = None
    prior_turn: str = ""
    rare_name: str = ""
    scored: bool = True

    def __post_init__(self) -> None:
        if self.language not in LANGUAGES:
            raise FixtureError(f"{self.case_id}: unknown language {self.language!r}")
        if self.kind not in KINDS:
            raise FixtureError(f"{self.case_id}: unknown kind {self.kind!r}")
        for status in (self.expected_status_on, self.expected_status_off):
            if status not in EXPECTED_STATUSES:
                raise FixtureError(f"{self.case_id}: unknown expected status {status!r}")
        if self.menu not in MENU_EXPECTATIONS:
            raise FixtureError(f"{self.case_id}: unknown menu expectation {self.menu!r}")
        if not self.turn.strip():
            raise FixtureError(f"{self.case_id}: turn must not be empty")


def _case(case_id: str, kind: str, turn: str, **values) -> MultilingualCase:
    arguments: dict = {
        "pairs_with": None,
        "gold": (),
        "poison": (),
        "never_resolved": (),
        "expected_status_on": "unresolved",
        "expected_status_off": "unresolved",
        "menu": "any",
    }
    arguments.update(values)
    return MultilingualCase(
        case_id=case_id, language=case_id.rsplit("-", 1)[1], kind=kind, turn=turn, **arguments
    )


CASES: tuple[MultilingualCase, ...] = (
    # -- M1 / N1: an English anchor's invented name inside native prose ------
    _case(
        "M1-de",
        "named_rare",
        "Ist der Quillmere schon für den Glasurbrand am Donnerstag gebucht?",
        gold=("r_quillmere_kiln",),
        expected_status_on="resolved",
        expected_status_off="partial",
        rare_name="Quillmere",
    ),
    _case(
        "N1-de",
        "named_rare_twin",
        "Hat Calloway schon gesagt, ob der Brennofen am Donnerstag für den Glasurbrand frei ist?",
        pairs_with="M1-de",
        never_resolved=("r_quillmere_kiln", "e_idris_calloway"),
        expected_status_on="partial",
        expected_status_off="partial",
        risk=4,
        rare_name="Calloway",
    ),
    _case(
        "M1-ru",
        "named_rare",
        "Надо проверить, не протекает ли Pellimore после зимы, прежде чем спускать его на воду.",
        gold=("r_pellimore_dinghy",),
        expected_status_on="resolved",
        expected_status_off="partial",
        rare_name="Pellimore",
    ),
    _case(
        "N1-ru",
        "named_rare_twin",
        "Спроси у Marchetti, не протекает ли лодка после зимы, прежде чем спускать её на воду.",
        pairs_with="M1-ru",
        never_resolved=("r_pellimore_dinghy", "e_hollis_marchetti"),
        expected_status_on="partial",
        expected_status_off="partial",
        risk=4,
        rare_name="Marchetti",
    ),
    # -- M2 / N2: no name; the page it is about sits low in the recent pool ---
    _case(
        "M2-en",
        "cross_language_menu",
        "When did we say the car should get its cold-weather wheels fitted?",
        gold=("n_winter_tyres",),
        poison=("n_thermal_curtains",),
        menu="gold_first",
    ),
    _case(
        "N2-en",
        "cross_language_menu_twin",
        "When did we say the bedroom should get its cold-weather curtains hung?",
        pairs_with="M2-en",
        poison=("n_winter_tyres",),
        risk=1,
    ),
    _case(
        "M2-de",
        "cross_language_menu",
        "Wie war das nochmal mit dem Teig, der über Nacht im Kühlschrank gehen soll?",
        gold=("n_sourdough_cold_proof",),
        poison=("n_photo_backup",),
        menu="gold_first",
    ),
    _case(
        "N2-de",
        "cross_language_menu_twin",
        "Wie war das nochmal mit den Fotos, die über Nacht auf die externe Festplatte sollen?",
        pairs_with="M2-de",
        poison=("n_sourdough_cold_proof",),
        risk=1,
    ),
    _case(
        "M2-ru",
        "cross_language_menu",
        "Напомни, какие упражнения для колена мне назначили и как часто их делать?",
        gold=("n_knee_rehab",),
        poison=("n_guitar_practice",),
        menu="gold_first",
    ),
    _case(
        "N2-ru",
        "cross_language_menu_twin",
        "Напомни, какие упражнения для гитары я себе составил и как часто их делать?",
        pairs_with="M2-ru",
        poison=("n_knee_rehab",),
        risk=1,
    ),
    _case(
        "M2-ja",
        "cross_language_menu",
        "ベランダのトマトの水やりって、どのくらいの頻度にするって決めたっけ？",
        gold=("n_tomato_watering",),
        poison=("n_aquarium_water",),
        menu="gold_first",
    ),
    _case(
        "N2-ja",
        "cross_language_menu_twin",
        "水槽の水替えって、どのくらいの頻度にするって決めたっけ？",
        pairs_with="M2-ja",
        poison=("n_tomato_watering",),
        risk=1,
    ),
    # -- M3: content-free "let's continue" -----------------------------------
    _case("M3-de", "content_free", "Machen wir weiter.", menu="recency", risk=3),
    _case("M3-ru", "content_free", "Продолжим.", menu="recency", risk=3),
    _case("M3-ja", "content_free", "続けましょう。", menu="recency", risk=3),
    _case("M3-et", "content_free", "Jätkame.", menu="recency", risk=3),
    # -- M4: a Russian turn about an English page, Russian pages unrelated ---
    _case(
        "M4-ru",
        "language_bias",
        "Опять плесень на потолке в ванной — что мы решили с этим делать?",
        gold=("n_bathroom_mould",),
        poison=("r_motoblok_kshatar", "n_ru_pelmeni"),
        menu="gold_first",
        risk=2,
    ),
    # -- M5 / N5: a Japanese anchor's name, then the name inside a compound --
    _case(
        "M5-ja",
        "cjk_named",
        "来月の合宿、山小屋の白樺をまた借りられるか確認してくれる？",
        gold=("r_shirakaba_hut",),
        expected_status_on="resolved",
        expected_status_off="partial",
        rare_name="白樺",
    ),
    _case(
        "N5-ja",
        "cjk_compound_twin",
        "駅前の白樺並木、今年は紅葉がきれいだったね。",
        pairs_with="M5-ja",
        never_resolved=("r_shirakaba_hut",),
        expected_status_on="partial",
        expected_status_off="partial",
        risk=5,
        rare_name="白樺",
    ),
    # -- M6: a resolved turn, then a same-language topic switch --------------
    _case(
        "M6-de",
        "continuity_switch",
        "Ganz anderes Thema: Wie lange muss ein Ei kochen, damit es wachsweich ist?",
        prior_turn="Ist das Lastenrad Fennholt schon aus der Werkstatt zurück?",
        never_resolved=("r_lastenrad_fennholt",),
        risk=6,
        rare_name="Fennholt",
    ),
    # -- M7: one accented word the ASCII catalogue splits --------------------
    _case(
        "M7-de",
        "carry_fragment",
        "Gebührenbescheid?",
        poison=("n_de_waste_fees",),
        risk=7,
        rare_name="Gebührenbescheid",
    ),
    _case(
        "M7-et",
        "carry_fragment",
        "Jätka.",
        poison=("n_estonian_phrases",),
        risk=7,
        rare_name="Jätka",
    ),
    # -- M8: a minority-language function word (residual; reported) ----------
    _case(
        "M8-de",
        "minority_function_word",
        "Gibt es schon Neuigkeiten zur Lieferung?",
        gold=("n_de_delivery",),
        risk=8,
        rare_name="zur Lieferung",
        scored=False,
    ),
)

CASE_IDS: tuple[str, ...] = tuple(case.case_id for case in CASES)


# --------------------------------------------------------------------------- #
# The corpus content. Every name is invented.
# --------------------------------------------------------------------------- #

#: (key, language, name, slug, summary, tags)
ENTITIES: tuple[tuple[str, str, str, str, str, tuple[str, ...]], ...] = (
    ("e_orla_venning", "en", "Orla Venning", "orla-venning", "Runs the Saturday bouldering club at the climbing wall.", ("climbing",)),
    ("e_tamsin_brevik", "en", "Tamsin Brevik", "tamsin-brevik", "Organises the neighbourhood book swap on the first Sunday of the month.", ("books",)),
    ("e_idris_calloway", "en", "Idris Calloway", "idris-calloway", "The accountant who prepares the household's annual tax return.", ("tax",)),
    ("e_maren_quist", "en", "Maren Quist", "maren-quist", "Infrastructure colleague who coordinates the on-call rota.", ("work",)),
    ("e_soren_halloway", "en", "Soren Halloway", "soren-halloway", "Runs the repair café in the library basement.", ("repair",)),
    ("e_juno_petrakis", "en", "Juno Petrakis", "juno-petrakis", "Friend organising the coastal walk and its hut bookings.", ("walking",)),
    ("e_wren_aldous", "en", "Wren Aldous", "wren-aldous", "Data engineer on the analytics team who maintains the reporting dashboards.", ("work",)),
    ("e_hollis_marchetti", "en", "Hollis Marchetti", "hollis-marchetti", "Neighbour who feeds the cat when the household is away.", ("neighbours",)),
    ("e_nessa_kilbride", "en", "Nessa Kilbride", "nessa-kilbride", "Coordinates the volunteer litter-pick along the canal.", ("volunteering",)),
    ("e_rafe_ostrander", "en", "Rafe Ostrander", "rafe-ostrander", "Mechanic at the community bike repair co-op.", ("repair",)),
    ("e_callum_treadwell", "en", "Callum Treadwell", "callum-treadwell", "Projectionist and programmer for the film club.", ("film",)),
    ("e_imogen_sallow", "en", "Imogen Sallow", "imogen-sallow", "Language-exchange partner who practises English on Thursday evenings.", ("languages",)),
    ("e_bram_oakhurst", "en", "Bram Oakhurst", "bram-oakhurst", "Chess club captain who organises the autumn league.", ("chess",)),
    ("e_delphine_arkwright", "en", "Delphine Arkwright", "delphine-arkwright", "Architect drawing up the loft conversion plans.", ("loft",)),
    ("e_henrike_brakemeier", "de", "Henrike Brakemeier", "henrike-brakemeier", "Kassenwartin im Tischtennisverein; sammelt Mitgliedsbeiträge.", ("verein",)),
    ("e_kemiri_bredal", "ru", "Кемири Брэдал", "kemiri-bredal", "Соседка по даче, собирает взносы за охрану посёлка.", ("dacha",)),
    ("e_mizuki_haruka", "ja", "水城 遥", "mizuki-haruka", "書道教室の先生。月謝は毎月初めに払う。", ("shodo",)),
)

ENTITY_NAMES: tuple[str, ...] = tuple(row[2] for row in ENTITIES)

#: (key, language, vault path, title, observation, tags) -- resource anchors.
RESOURCES: tuple[tuple[str, str, str, str, str, tuple[str, ...]], ...] = (
    ("r_quillmere_kiln", "en", "Knowledge Base/Products/Quillmere kiln.md", "Quillmere kiln",
     "The Quillmere kiln at the community studio fires stoneware to cone six; firings are booked on the studio sheet a week ahead.",
     ("resource", "ceramics")),
    ("r_pellimore_dinghy", "en", "Knowledge Base/Products/Pellimore dinghy.md", "Pellimore dinghy",
     "The Pellimore is a two-person sailing dinghy kept at the lake club; the hull is checked for leaks every spring before launch.",
     ("resource", "sailing")),
    ("r_espresso_machine", "en", "Knowledge Base/Products/Espresso machine.md", "Espresso machine",
     "Dual-boiler espresso machine; descale it monthly and replace the group gasket once a year.", ("resource", "kitchen")),
    ("r_label_printer", "en", "Knowledge Base/Products/Label printer.md", "Label printer",
     "Thermal label printer used for parcel labels and pantry jars.", ("resource", "office")),
    ("r_resin_printer", "en", "Knowledge Base/Products/Resin printer.md", "Resin printer",
     "Resin printer for tabletop miniatures; the vat film is replaced after about forty prints.", ("resource", "hobby")),
    ("r_sewing_machine", "en", "Knowledge Base/Products/Sewing machine.md", "Sewing machine",
     "Heavy-duty sewing machine; the walking foot handles canvas and denim repairs.", ("resource", "sewing")),
    ("r_telescope", "en", "Knowledge Base/Products/Dobsonian telescope.md", "Dobsonian telescope",
     "Eight-inch reflector; collimate it before each observing session and store it with the dust cap on.", ("resource", "astronomy")),
    ("r_pressure_washer", "en", "Knowledge Base/Products/Pressure washer.md", "Pressure washer",
     "Electric pressure washer for the patio slabs and the fence panels.", ("resource", "outdoor")),
    ("r_camper_trailer", "en", "Knowledge Base/Products/Camper trailer.md", "Camper trailer",
     "Folding camper trailer; the awning poles live in the garage loft.", ("resource", "camping")),
    ("r_survey_drone", "en", "Knowledge Base/Products/Survey drone.md", "Survey drone",
     "Small quadcopter used to inspect the roof gutters; its batteries are stored half charged.", ("resource", "house")),
    ("r_projector", "en", "Knowledge Base/Products/Portable projector.md", "Portable projector",
     "Short-throw projector the film club borrows for outdoor screenings.", ("resource", "film")),
    ("r_chainsaw", "en", "Knowledge Base/Products/Battery chainsaw.md", "Battery chainsaw",
     "Cordless chainsaw for clearing fallen branches; sharpen the chain every third charge.", ("resource", "outdoor")),
    ("r_soldering_station", "en", "Knowledge Base/Products/Soldering station.md", "Soldering station",
     "Temperature-controlled soldering station for the repair café; tips are replaced when pitted.", ("resource", "repair")),
    ("r_standing_desk", "en", "Knowledge Base/Products/Standing desk.md", "Standing desk",
     "Motorised standing desk; the controller forgets its presets after a power cut.", ("resource", "office")),
    ("r_robot_vacuum", "en", "Knowledge Base/Products/Robot vacuum.md", "Robot vacuum",
     "Robot vacuum that maps the ground floor; the brush roll tangles on long rugs.", ("resource", "house")),
    ("r_home_router", "en", "Knowledge Base/Systems/Home router.md", "Home router",
     "Fibre router in the hallway cupboard; the admin login is kept in the password manager.", ("resource", "network")),
    ("r_wood_lathe", "en", "Knowledge Base/Systems/Wood lathe.md", "Wood lathe",
     "Shared wood lathe at the makerspace; lock the tool rest before switching it on.", ("resource", "makerspace")),
    ("r_laser_cutter", "en", "Knowledge Base/Systems/Laser cutter.md", "Laser cutter",
     "Makerspace laser cutter; acrylic only on the honeycomb bed and never PVC.", ("resource", "makerspace")),
    ("r_lastenrad_fennholt", "de", "Knowledge Base/Products/Lastenrad Fennholt.md", "Lastenrad Fennholt",
     "Lastenrad Fennholt: Wocheneinkauf, Kettenwechsel alle drei Monate in der Werkstatt.",
     ("resource", "fahrrad")),
    ("r_motoblok_kshatar", "ru", "Knowledge Base/Products/Мотоблок Кшатар.md", "Мотоблок Кшатар",
     "Мотоблок Кшатар стоит в сарае на даче; им косим траву вдоль забора.", ("resource", "dacha")),
    ("r_shirakaba_hut", "ja", "Knowledge Base/Products/白樺.md", "白樺",
     "山小屋「白樺」は合宿のときに借りる。定員は十二人で、予約は二か月前までに管理人へ連絡する。", ("resource", "gasshuku")),
)

#: (key, language, title, slug, category, observation, tags). A tag ``hub``
#: makes the note a hub anchor; every other note is not an anchor at all.
NOTES: tuple[tuple[str, str, str, str, str, str, tuple[str, ...]], ...] = (
    # Hubs.
    ("h_book_club", "en", "Book club hub", "book-club-hub", "hub",
     "Monthly book club: the reading list, the host rota and short meeting notes.", ("hub", "books")),
    ("h_loft_conversion", "en", "Loft conversion hub", "loft-conversion-hub", "hub",
     "Permits, quotes and drawings for converting the loft into a study.", ("hub", "loft")),
    ("h_coastal_walk", "en", "Coastal walk hub", "coastal-walk-hub", "hub",
     "Stages, huts and luggage transfer for the coastal path walk.", ("hub", "walking")),
    ("h_litter_pick", "en", "Litter-pick hub", "litter-pick-hub", "hub",
     "Routes, kit and volunteer sign-ups for the canal litter-pick.", ("hub", "volunteering")),
    ("h_tax_return", "en", "Tax return hub", "tax-return-hub", "hub",
     "Receipts, deadlines and the accountant's checklist for the annual return.", ("hub", "tax")),
    ("h_language_exchange", "en", "Language exchange hub", "language-exchange-hub", "hub",
     "Weekly language exchange: partners, topics and the café venue.", ("hub", "languages")),
    ("h_repair_cafe", "en", "Repair café hub", "repair-cafe-hub", "hub",
     "Volunteer rota, tool list and repair log for the library repair café.", ("hub", "repair")),
    ("h_film_club", "en", "Film club hub", "film-club-hub", "hub",
     "Screening schedule, licences and snacks for the film club.", ("hub", "film")),
    ("h_stargazing", "en", "Stargazing hub", "stargazing-hub", "hub",
     "Dark-sky sites, moon phases and the telescope checklist.", ("hub", "astronomy")),
    ("h_makerspace", "en", "Makerspace hub", "makerspace-hub", "hub",
     "Inductions, machine bookings and material rules at the makerspace.", ("hub", "makerspace")),
    ("h_chess_club", "en", "Chess club hub", "chess-club-hub", "hub",
     "League fixtures, team sheets and opening preparation for the chess club.", ("hub", "chess")),
    ("h_tischtennis", "de", "Tischtennisverein", "tischtennisverein", "hub",
     "Trainingszeiten, Mannschaftsaufstellung, Hallenschlüssel.", ("hub", "verein")),
    ("h_dacha", "ru", "Дача", "dacha", "hub",
     "Ремонт крыши сарая, вывоз мусора и график поездок на дачу.", ("hub", "dacha")),
    ("h_shodo", "ja", "書道教室", "shodo-kyoshitsu", "hub",
     "書道教室の予定、月謝、課題の提出日をまとめる。", ("hub", "shodo")),
    # Case pages.
    ("n_winter_tyres", "en", "Winter tyre changeover", "winter-tyre-changeover", "action",
     "Fit the winter tyres once nights stay below seven degrees; the garage wants a week's notice for the swap.",
     ("maintenance",)),
    ("n_thermal_curtains", "en", "Thermal curtains", "thermal-curtains", "action",
     "Hang the thermal curtains in the bedroom when the heating comes on; they stop the draught from the old sash window.",
     ("house",)),
    ("n_sourdough_cold_proof", "en", "Sourdough cold proof", "sourdough-cold-proof", "technique",
     "Shape the loaf in the evening and let it proof overnight in the fridge; the crumb is more open and it bakes before breakfast.",
     ("baking",)),
    ("n_photo_backup", "en", "Nightly photo backup", "nightly-photo-backup", "technique",
     "The phone photos are copied to the external drive every night, and the drive is swapped with the offsite copy each month.",
     ("computers",)),
    ("n_knee_rehab", "en", "Knee rehab routine", "knee-rehab-routine", "technique",
     "Three sets of wall sits and slow step-downs every other day; skip a session if the kneecap aches the next morning.",
     ("health",)),
    ("n_guitar_practice", "en", "Guitar practice routine", "guitar-practice-routine", "technique",
     "Ten minutes of scales, then chord changes against a metronome, most evenings before dinner.", ("music",)),
    ("n_tomato_watering", "en", "Balcony tomato watering", "balcony-tomato-watering", "technique",
     "Water the balcony tomatoes early in the morning, skip the day after heavy rain and add liquid feed once a week.",
     ("balcony",)),
    ("n_aquarium_water", "en", "Aquarium water changes", "aquarium-water-changes", "technique",
     "Replace a quarter of the aquarium water every Sunday and test the nitrate level before topping up.", ("pets",)),
    ("n_bathroom_mould", "en", "Bathroom ceiling mould", "bathroom-ceiling-mould", "technique",
     "Run the extractor fan for twenty minutes after every shower and wipe the ceiling with diluted vinegar each fortnight.",
     ("house",)),
    ("n_estonian_phrases", "en", "Estonian practice phrases", "estonian-practice-phrases", "fact",
     "Phrases for the language exchange: tere means hello, aitäh means thank you, jätka means continue and head aega means goodbye.",
     ("languages",)),
    # German prose notes. Six of them, so the common function words are
    # common by document frequency here too; `zur` stays on one page (M8).
    ("n_de_waste_fees", "de", "Müllgebühren", "muellgebuehren", "fact",
     "Der Gebührenbescheid für die Müllabfuhr ist im August gekommen, und das Geld wird jetzt jedes Quartal mit Lastschrift abgebucht; wir haben es am Montag im Ordner Haushalt abgelegt.",
     ("haushalt",)),
    ("n_de_delivery", "de", "Regalbretter", "regalbretter", "fact",
     "Die Regalbretter sind bestellt, der Hinweis zur Lieferung ist am Dienstag mit der Post gekommen, und wir räumen für den Spediteur im Flur das alte Regal auf.",
     ("haus",)),
    ("n_de_utility_bill", "de", "Nebenkostenabrechnung", "nebenkostenabrechnung", "fact",
     "Die Nebenkostenabrechnung ist im Juni gekommen, und wir überweisen die Nachzahlung für das Jahr bis Ende Juli an die Verwaltung; es steht alles auf der zweiten Seite, mit den Zählerständen.",
     ("haushalt",)),
    ("n_de_parents_evening", "de", "Elternabend", "elternabend", "fact",
     "Der Elternabend der Grundschule ist am zweiten Dienstag im Oktober, und wir bringen die Liste mit den Terminen für das Halbjahr auf Papier mit, es reicht eine Kopie.",
     ("schule",)),
    ("n_de_holiday_flat", "de", "Ferienwohnung", "ferienwohnung", "fact",
     "Die Ferienwohnung ist für die erste Augustwoche reserviert, und der Schlüssel liegt im Kasten neben der Tür; wir kommen am Abend mit der Bahn an, und das Auto lassen wir zu Hause, es ist alles in Laufweite.",
     ("urlaub",)),
    ("n_de_tool_library", "de", "Werkzeugverleih", "werkzeugverleih", "fact",
     "Im Werkzeugverleih kann man die Bohrmaschine für das Wochenende ausleihen, und die Kaution ist bar an der Kasse zu zahlen; wir geben sie am Montag mit den Bohrern ab, und es läuft alles auf den Mitgliedsausweis.",
     ("haus",)),
    ("n_ru_pelmeni", "ru", "Пельмени на зиму", "pelmeni-na-zimu", "fact",
     "Лепим пельмени партиями по сто штук, замораживаем на подносе и пересыпаем в пакеты.", ("kitchen",)),
    ("n_ru_passport", "ru", "Замена загранпаспорта", "zamena-zagranpasporta", "fact",
     "Заявление подаём через портал; фото делаем заранее, паспорт готовят около месяца.", ("documents",)),
    ("n_ru_furniture", "ru", "Вывоз старой мебели", "vyvoz-staroy-mebeli", "fact",
     "Старый диван и шкаф вывозит городская служба по заявке; заказывать нужно за неделю.", ("house",)),
    ("n_ja_emergency_bag", "ja", "防災リュックの中身", "bousai-rucksack", "fact",
     "水と非常食を三日分、懐中電灯と予備の電池、常備薬のメモを入れておく。", ("house",)),
    ("n_ja_brush_care", "ja", "筆の手入れ", "fude-no-teire", "fact",
     "使った筆はぬるま湯で墨を落とし、穂先を整えてから陰干しする。", ("shodo",)),
    # Fillers: the rest of an ordinary English-majority vault.
    ("f_password_manager", "en", "Password manager migration", "password-manager-migration", "action",
     "Move the shared logins into the family vault before the old subscription lapses at the end of the month.", ("computers",)),
    ("f_recycling_days", "en", "Recycling collection days", "recycling-collection-days", "fact",
     "Glass goes out on alternate Tuesdays; cardboard must be flattened or the crew leaves it.", ("house",)),
    ("f_coffee_grinder", "en", "Coffee grinder burr settings", "coffee-grinder-burr-settings", "fact",
     "Setting nine suits the filter brewer; espresso needs setting three and a finer tweak for dark roasts.", ("kitchen",)),
    ("f_board_game_night", "en", "Board game night", "board-game-night", "fact",
     "Hosts pick two medium-weight games; newcomers get the short rules summary first.", ("games",)),
    ("f_wifi_mesh", "en", "Wi-Fi mesh node", "wifi-mesh-node", "technique",
     "The hallway mesh node drops after firmware updates; restart the main unit before the satellite.", ("network",)),
    ("f_chess_openings", "en", "Chess opening repertoire", "chess-opening-repertoire", "fact",
     "Play the Caro-Kann against e4 and the Queen's Gambit Declined against d4 in league games.", ("chess",)),
    ("f_budget_categories", "en", "Budget spreadsheet categories", "budget-spreadsheet-categories", "fact",
     "Groceries, transport and subscriptions get monthly caps; anything above fifty pounds needs a note.", ("money",)),
    ("f_podcast_queue", "en", "Podcast listening queue", "podcast-listening-queue", "fact",
     "Queue the history series for the commute and the interview shows for weekend chores.", ("media",)),
    ("f_museum_membership", "en", "Museum membership", "museum-membership", "fact",
     "The family membership covers two adults and three children and includes free exhibition previews.", ("family",)),
    ("f_keyboard_switches", "en", "Keyboard switch comparison", "keyboard-switch-comparison", "finding",
     "Tactile switches for typing all day; linear ones felt too light on the test board.", ("computers",)),
    ("f_travel_adapters", "en", "Travel adapter checklist", "travel-adapter-checklist", "fact",
     "Pack two universal adapters and the short extension lead; hotel sockets are always behind the bed.", ("travel",)),
    ("f_shoe_repair", "en", "Shoe repair", "shoe-repair", "fact",
     "The cobbler on the high street resoles leather boots in a week; heels are done while you wait.", ("errands",)),
    ("f_library_card", "en", "Library card renewal", "library-card-renewal", "fact",
     "Cards renew online every two years; the reservation limit is twelve items.", ("books",)),
    ("f_gift_ideas", "en", "Gift ideas for the nephew", "gift-ideas-nephew", "fact",
     "He is into model rockets and adventure novels this year; avoid anything with small batteries.", ("family",)),
    ("f_home_insurance", "en", "Home insurance renewal", "home-insurance-renewal", "action",
     "The renewal quote rose by a fifth; compare two brokers before the policy rolls over in November.", ("money",)),
    ("f_phone_contract", "en", "Phone contract comparison", "phone-contract-comparison", "finding",
     "A SIM-only plan with thirty gigabytes is enough; keep the handset for another year.", ("money",)),
    ("f_tram_pass", "en", "Tram pass top-up", "tram-pass-top-up", "fact",
     "The monthly pass is cheaper after sixteen journeys; top it up before the first working day.", ("travel",)),
    ("f_fountain_pen_inks", "en", "Fountain pen inks", "fountain-pen-inks", "finding",
     "Blue-black iron gall ink for the journal; the brown ink feathers on cheap paper.", ("stationery",)),
    ("f_crossword_tips", "en", "Crossword solving tips", "crossword-solving-tips", "fact",
     "Start with the anagrams and the short clues; the setter hides abbreviations in the long ones.", ("games",)),
    ("f_chimney_sweep", "en", "Chimney sweep appointment", "chimney-sweep-appointment", "action",
     "The sweep comes every September before the wood burner is lit; book the visit in July.", ("house",)),
    ("f_spice_rack", "en", "Spice rack inventory", "spice-rack-inventory", "fact",
     "Smoked paprika and cumin run out first; whole spices keep far longer than ground ones.", ("kitchen",)),
    ("f_party_rsvps", "en", "Party RSVP list", "party-rsvp-list", "fact",
     "Thirty-two guests confirmed; three still owe a reply about the vegetarian option.", ("family",)),
    ("f_donation_receipts", "en", "Charity donation receipts", "charity-donation-receipts", "fact",
     "Keep the gift-aid receipts in the tax folder; the accountant asks for them in January.", ("money",)),
    ("f_puzzle_swap", "en", "Puzzle swap shelf", "puzzle-swap-shelf", "fact",
     "The café shelf takes complete jigsaws only; tape the box lid with the piece count.", ("games",)),
    ("f_lentil_soup", "en", "Lentil soup batch", "lentil-soup-batch", "technique",
     "A double batch of red lentil soup fills eight tubs; it keeps in the freezer for three months.", ("kitchen",)),
    ("f_smoke_alarms", "en", "Smoke alarm checks", "smoke-alarm-checks", "technique",
     "Test every alarm on the first of the month and swap the batteries each spring.", ("house",)),
    ("f_stamp_catalogue", "en", "Stamp collection catalogue", "stamp-collection-catalogue", "fact",
     "The album is sorted by country; duplicates go in the envelope for the club auction.", ("hobby",)),
    ("f_school_uniform", "en", "School uniform sizes", "school-uniform-sizes", "fact",
     "Order the next size up for jumpers; the trousers still fit until the spring term.", ("family",)),
    ("f_solar_output", "en", "Solar panel output", "solar-panel-output", "finding",
     "Summer days produce around eighteen kilowatt hours; the darkest months fall below four.", ("house",)),
    ("f_noise_complaint", "en", "Noise complaint log", "noise-complaint-log", "fact",
     "Late-night drilling next door on three weekdays; the council wants the dates and times written down.", ("house",)),
    ("f_python_course", "en", "Online Python course", "online-python-course", "fact",
     "Finished the module on generators; the next one covers async code and takes two weeks.", ("learning",)),
    ("f_hiking_boots", "en", "Hiking boot resoling", "hiking-boot-resoling", "fact",
     "The soles can be replaced twice before the uppers crack; send the boots in spring.", ("walking",)),
    ("f_bookshelf_anchors", "en", "Bookshelf wall anchors", "bookshelf-wall-anchors", "technique",
     "Use the heavy-duty plugs on the plasterboard wall and anchor the top of each bookcase.", ("house",)),
    ("f_train_timetable", "en", "Coast train timetable", "coast-train-timetable", "fact",
     "The early train connects with the ferry; the last direct service back leaves at nine.", ("travel",)),
    ("f_knitting_pattern", "en", "Knitting pattern notes", "knitting-pattern-notes", "technique",
     "The cable jumper needs a size smaller needle for the ribbing; mark every sixth row.", ("craft",)),
    ("f_email_filters", "en", "Email filter rules", "email-filter-rules", "fact",
     "Newsletters skip the inbox; invoices from the energy supplier go to the bills folder.", ("computers",)),
    ("f_parking_permit", "en", "Residents parking permit", "residents-parking-permit", "fact",
     "The permit renews in March; visitor vouchers come in books of ten.", ("errands",)),
    ("f_dentist", "en", "Dentist check-up", "dentist-check-up", "fact",
     "Check-ups every six months; the hygienist appointment is booked separately.", ("family",)),
    ("f_desk_lamp", "en", "Desk lamp bulbs", "desk-lamp-bulbs", "finding",
     "Warm white bulbs for evening work; daylight bulbs only in the craft room.", ("house",)),
    ("f_moving_boxes", "en", "Moving box stash", "moving-box-stash", "fact",
     "Twenty flat-packed boxes wait in the loft for the next move or the jumble sale.", ("house",)),
    ("f_plum_jam", "en", "Plum jam", "plum-jam", "technique",
     "Two kilos of plums make six jars; add the pectin only if the set test fails.", ("kitchen",)),
    ("f_audiobook_credits", "en", "Audiobook credits", "audiobook-credits", "fact",
     "One credit a month; the long fantasy series is the best value per hour.", ("media",)),
)

#: Records collections: (key, manifest path, exomem id, title, claims, items).
RECORDS: tuple[tuple[str, str, str, str, tuple[str, ...], tuple[tuple[str, dict[str, object]], ...]], ...] = (
    (
        "c_meter_readings",
        "Knowledge Base/Records/Meter Readings/_collection.md",
        "70000000-0000-4000-8000-000000000001",
        "Electricity meter readings",
        ("electricity meter", "meter readings"),
        (
            ("70000000-0000-4000-8000-000000000011", {"observed_on": "2026-08-01", "subject": "main-meter", "state": "18432 kWh"}),
            ("70000000-0000-4000-8000-000000000012", {"observed_on": "2026-09-01", "subject": "main-meter", "state": "18710 kWh"}),
        ),
    ),
    (
        "c_book_loans",
        "Knowledge Base/Records/Book Loans/_collection.md",
        "70000000-0000-4000-8000-000000000002",
        "Library book loans",
        ("library loans", "book loans"),
        (
            ("70000000-0000-4000-8000-000000000021", {"observed_on": "2026-09-05", "subject": "borrowed-atlas", "state": "due 2026-09-26"}),
        ),
    ),
)

#: Planning items: (key, manifest path, exomem id, collection title, plan id, item title).
PLANS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("p_loft_quotes", "Knowledge Base/Planning/Loft Conversion/_collection.md",
     "70000000-0000-4000-8000-000000000003", "Loft conversion plan",
     "70000000-0000-4000-8000-000000000031", "Get three quotes for the loft insulation"),
    ("p_luggage_transfer", "Knowledge Base/Planning/Coastal Walk/_collection.md",
     "70000000-0000-4000-8000-000000000004", "Coastal walk plan",
     "70000000-0000-4000-8000-000000000041", "Book the luggage transfer for the coastal walk"),
)

#: Every page's logical key -> the language it is written in.
KEY_LANGUAGES: dict[str, str] = {
    **{row[0]: row[1] for row in ENTITIES},
    **{row[0]: row[1] for row in RESOURCES},
    **{row[0]: row[1] for row in NOTES},
    **{row[0]: "en" for row in RECORDS},
    **{row[0]: "en" for row in PLANS},
}

#: The 24-page recent pool, most recent first. The top eight are the recency
#: menu and are unrelated to every case; menu golds and their poisons sit
#: below them, interleaved so that recency alone favours neither.
POOL_ORDER: tuple[str, ...] = (
    "f_password_manager",
    "f_recycling_days",
    "f_coffee_grinder",
    "f_board_game_night",
    "f_wifi_mesh",
    "f_chess_openings",
    "f_budget_categories",
    "f_podcast_queue",
    "n_winter_tyres",
    "n_ru_pelmeni",
    "n_sourdough_cold_proof",
    "n_guitar_practice",
    "f_museum_membership",
    "n_knee_rehab",
    "n_aquarium_water",
    "r_motoblok_kshatar",
    "n_bathroom_mould",
    "n_photo_backup",
    "n_ja_emergency_bag",
    "n_tomato_watering",
    "n_thermal_curtains",
    "f_keyboard_switches",
    "f_travel_adapters",
    "f_shoe_repair",
)

#: Every turn a corpus page must never contain. A carry-fragment turn is its
#: one word, which the design places on exactly one page on purpose.
ALL_TURNS: tuple[str, ...] = tuple(
    text for case in CASES for text in (case.turn, case.prior_turn) if text.strip()
)
LEAK_CHECKED_TURNS: tuple[str, ...] = tuple(
    text
    for case in CASES
    if case.kind != "carry_fragment"
    for text in (case.turn, case.prior_turn)
    if text.strip()
)


def case_by_id(case_id: str) -> MultilingualCase:
    for case in CASES:
        if case.case_id == case_id:
            return case
    raise FixtureError(f"unknown multilingual case id {case_id!r}")


def fixture_set_digest(cases: Iterable[MultilingualCase] = CASES) -> str:
    """Stable sha256 over every field of every case, ordered by case id."""

    rows = sorted(
        ({field.name: getattr(case, field.name) for field in fields(MultilingualCase)} for case in cases),
        key=lambda row: row["case_id"],
    )
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def assert_manifest_consistent(cases: tuple[MultilingualCase, ...] = CASES) -> None:
    """Refuse duplicate ids, a positive without a same-language twin, a twin
    of the wrong language or kind, or a key no page declares."""

    ids = [case.case_id for case in cases]
    if len(set(ids)) != len(ids):
        raise FixtureError("duplicate case_id in the multilingual set")
    by_id = {case.case_id: case for case in cases}
    for case in cases:
        referenced = (*case.gold, *case.poison, *case.never_resolved)
        unknown = [key for key in referenced if key not in KEY_LANGUAGES]
        if unknown:
            raise FixtureError(f"{case.case_id}: keys {unknown} name no corpus page")
        if case.menu == "gold_first" and not case.gold:
            raise FixtureError(f"{case.case_id}: a gold-first menu case needs gold")
        if case.kind not in TWIN_OF:
            if case.pairs_with is not None:
                raise FixtureError(f"{case.case_id}: only a twin pairs with a case")
            continue
        positive = by_id.get(case.pairs_with or "")
        if positive is None or positive.kind != TWIN_OF[case.kind]:
            raise FixtureError(f"{case.case_id}: pairs_with must name a {TWIN_OF[case.kind]} case")
        if positive.language != case.language:
            raise FixtureError(f"{case.case_id}: a twin must share its case's language")
        if not set(case.gold).isdisjoint(positive.gold):
            raise FixtureError(f"{case.case_id}: a twin's gold overlaps its case's gold")
        guarded = case.poison if case.kind == "cross_language_menu_twin" else case.never_resolved
        if not set(positive.gold) <= set(guarded):
            raise FixtureError(f"{case.case_id}: a twin must guard its case's gold")
    for case in cases:
        if case.kind in POSITIVE_KINDS and not any(twin.pairs_with == case.case_id for twin in cases):
            raise FixtureError(f"{case.case_id}: a positive needs a same-language negative twin")


# --------------------------------------------------------------------------- #
# The corpus build
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MultilingualCorpusManifest:
    corpus_id: str
    fixture_set_digest: str
    key_to_path: dict[str, str]
    #: vault-relative path -> mtime_ns for every fixture page, the shape
    #: `working_set._recent_mtimes` returns.
    recency: dict[str, int]
    corpus_hash: str


_RECORDS_FIELDS = (
    "    observed_on:\n"
    "      type: date\n"
    "      required: true\n"
    "    subject:\n"
    "      type: string\n"
    "      required: true\n"
    "    state:\n"
    "      type: string"
)


def _apply_recency(root: Path, key_to_path: dict[str, str]) -> dict[str, int]:
    """Stamp every fixture page with its explicit mtime; every other page is
    older than all of them. One hour apart throughout, so nothing is a burst."""

    ranked = [*POOL_ORDER, *sorted(key for key in key_to_path if key not in POOL_ORDER)]
    recency: dict[str, int] = {}
    for rank, key in enumerate(ranked):
        mtime = RECENCY_BASE_NS - rank * RECENCY_SPACING_NS
        relative = key_to_path[key]
        os.utime(root / relative, ns=(mtime, mtime))
        recency[relative] = mtime
    oldest = RECENCY_BASE_NS - len(ranked) * RECENCY_SPACING_NS
    others = sorted(
        path
        for path in (root / "Knowledge Base").rglob("*.md")
        if path.relative_to(root).as_posix() not in recency
    )
    for offset, path in enumerate(others):
        mtime = oldest - offset * RECENCY_SPACING_NS
        os.utime(path, ns=(mtime, mtime))
    return recency


def _build_corpus_in_process(root: Path) -> MultilingualCorpusManifest:
    from exomem.init import init_vault

    root = Path(root)
    init_vault(root)
    key_to_path: dict[str, str] = {}
    for key, _language, name, slug, summary, tags in ENTITIES:
        key_to_path[key] = _entity(root, name=name, slug=slug, summary=summary, tags=tags)
    for key, _language, path, title, observation, tags in RESOURCES:
        key_to_path[key] = _governed_resource(root, path=path, title=title, observation=observation, tags=tags)
    for key, _language, title, slug, category, observation, tags in NOTES:
        key_to_path[key] = _compiled_note(
            root, title=title, slug=slug, observation=observation, category=category, tags=tags
        )
    for key, manifest_path, exomem_id, title, claims, items in RECORDS:
        key_to_path[key] = _create_records_collection(
            root,
            manifest_path=manifest_path,
            manifest_text=_records_manifest(
                exomem_id=exomem_id,
                title=title,
                source="Items",
                claims=claims,
                fields=_RECORDS_FIELDS,
                natural_key="observed_on, subject",
            ),
            items=items,
        )
    for key, manifest_path, exomem_id, collection_title, plan_id, item_title in PLANS:
        key_to_path[key] = _create_planning_item(
            root,
            manifest_path=manifest_path,
            manifest_text=_planning_manifest(exomem_id=exomem_id, title=collection_title),
            plan_id=plan_id,
            title=item_title,
        )

    leaks = find_verbatim_leaks(root, turns=LEAK_CHECKED_TURNS) + find_normalized_leaks(
        root, turns=LEAK_CHECKED_TURNS
    )
    if leaks:
        raise FixtureError(f"multilingual fixture turn(s) leaked into the corpus: {leaks!r}")
    recency = _apply_recency(root, key_to_path)
    return MultilingualCorpusManifest(
        corpus_id=CORPUS_ID,
        fixture_set_digest=fixture_set_digest(),
        key_to_path=dict(sorted(key_to_path.items())),
        recency=dict(sorted(recency.items())),
        corpus_hash=_corpus_hash(root),
    )


def _run_isolated_build_request(request_path: str, result_path: str) -> None:
    """Child-process entry point for one isolated corpus build."""

    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    manifest = _build_corpus_in_process(Path(request["root"]))
    Path(result_path).write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def build_corpus(root: Path, *, timeout: float = 900.0) -> MultilingualCorpusManifest:
    """Build the corpus in a child with private state, leases, config and logs,
    exactly as the English set's ``build_corpus`` does: the caller's environment
    and singletons are never rebound, and a writer refusal fails the build."""

    root = Path(root).resolve()
    repository = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory(prefix="exomem-context-activation-multilingual-") as runtime_raw:
        runtime = Path(runtime_raw)
        directories = {name: runtime / name for name in ("state", "xdg-state", "logs", "call-ledger", "writer-lease", "tmp")}
        for directory in directories.values():
            directory.mkdir(parents=True, exist_ok=True)
        request, result = runtime / "request.json", runtime / "result.json"
        request.write_text(json.dumps({"root": str(root)}), encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if not key.startswith("EXOMEM_")}
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    path
                    for path in (str(repository / "src"), str(repository / "benchmarks"), environment.get("PYTHONPATH", ""))
                    if path
                ),
                "EXOMEM_STATE_ROOT": str(directories["state"]),
                "EXOMEM_CONFIG_PATH": str(runtime / "config.json"),
                "EXOMEM_LOG_DIR": str(directories["logs"]),
                "EXOMEM_CALL_LEDGER_DIR": str(directories["call-ledger"]),
                "EXOMEM_WRITER_LEASE_STATE_DIR": str(directories["writer-lease"]),
                "EXOMEM_LEASE_COORDINATOR_DB": str(runtime / "lease-coordinator.sqlite"),
                "EXOMEM_VAULT_PATH": str(root),
                "EXOMEM_DISABLE_EMBEDDINGS": "1",
                "EXOMEM_DISABLE_GRAPH_DRAIN": "1",
                "EXOMEM_DISABLE_GRAPH_SCHEDULING": "1",
                "XDG_STATE_HOME": str(directories["xdg-state"]),
                "TMPDIR": str(directories["tmp"]),
            }
        )
        code = (
            "from epistemic.corpora.context_activation_multilingual import _run_isolated_build_request; "
            f"_run_isolated_build_request({str(request)!r}, {str(result)!r})"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise FixtureError(f"isolated multilingual corpus build exceeded {timeout:.0f} seconds") from error
        if completed.returncode != 0:
            detail = completed.stderr[-4000:].strip() or completed.stdout[-4000:].strip()
            raise FixtureError(f"isolated multilingual corpus build failed with exit {completed.returncode}: {detail}")
        if not result.is_file():
            raise FixtureError("isolated multilingual corpus build returned no manifest")
        return MultilingualCorpusManifest(**json.loads(result.read_text(encoding="utf-8")))


__all__ = [
    "ALL_TURNS",
    "CASES",
    "CASE_IDS",
    "CORPUS_ID",
    "ENTITIES",
    "ENTITY_NAMES",
    "FIXTURE_SET_ID",
    "KEY_LANGUAGES",
    "KINDS",
    "LANGUAGES",
    "LEAK_CHECKED_TURNS",
    "MENU_EXPECTATIONS",
    "NOTES",
    "PLANS",
    "POOL_ORDER",
    "POSITIVE_KINDS",
    "RECENCY_BASE_NS",
    "RECENCY_SPACING_NS",
    "RECENT_MENU_SIZE",
    "RECENT_POOL_SIZE",
    "RECORDS",
    "RESOURCES",
    "TWIN_OF",
    "FixtureError",
    "MultilingualCase",
    "MultilingualCorpusManifest",
    "assert_manifest_consistent",
    "build_corpus",
    "case_by_id",
    "fixture_set_digest",
]
