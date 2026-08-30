"""Render the founder judging sheet for the no-nudge calibration study.

Derives everything from benchmarks/epistemic/corpora/no_nudge.py (the governed
generators), so the sheet is reproducible. Pages are presented under neutral
aliases with twins interleaved and unlabelled, per the protocol; page and
referent ids are scrubbed from body text so the aliases hold. The alias key is
written to a separate file the annotator does not need.

Run: PYTHONPATH=<repo>/benchmarks python gen_calibration_sheet.py SHEET.md KEY.md
"""
import sys
from pathlib import Path

from epistemic.corpora import no_nudge as nn

OUT = Path(sys.argv[1])
KEY = Path(sys.argv[2])

F20_ORDER = ["f20-twin-tangent", "f20-subject", "f20-twin-log", "f20-twin-bounded", "f20-twin-hub"]
F20_ALIAS = {page: f"Page {chr(65+i)}" for i, page in enumerate(F20_ORDER)}
F21_ORDER = ["f21-twin-incidental", "f21-subject-lower", "f21-subject-cyrillic"]
F21_ALIAS = {r: f"Referent {i+1}" for i, r in enumerate(F21_ORDER)}

def scrub(text, mapping):
    for real, alias in mapping.items():
        text = text.replace(real, alias.lower().replace(" ", "-"))
    return text

corpus = nn.f20_corpus(surfaced=False)
units = {i.id: i for i in corpus.items}
rels = corpus.relations

def links_for(uid):
    out = []
    for r in rels:
        if r.subject == uid:
            out.append(("->", r.object))
        elif r.object == uid:
            out.append(("<-", r.subject))
    return out

def anon(uid, page):
    for p, a in F20_ALIAS.items():
        if uid.startswith(p + "-u"):
            return f"{a} write {int(uid.split('-u')[1]) + 1}"
    return "an external page"

lines = []
A = lines.append
A("# No-nudge calibration — founder judging sheet")
A("")
A("**Study:** no-nudge emergence calibration (amendment sequence 2, small-cohort fallback).")
A("**Your role:** answer as this vault's owner. Work in one sitting, in order, without")
A("skipping ahead — each section replays a page as a sequence of writes, and the honest")
A("answer is the FIRST write at which you would genuinely want the system to step in.")
A("There are no right answers; 'never' is data, not a failure.")
A("")
A("Answers go in the **answer block at the very end** — copy it back to me filled in.")
A("")
A("---")
A("")
A("## Part 1 — structure accumulating on a page (5 pages)")
A("")
A("Each page below receives six writes, shown in order. Each write adds one durable unit")
A("(its text, its category label, its anchor) and sometimes links. After reading each")
A("write, ask yourself: *'Would I, as this vault's owner, now want the system to propose")
A("restructuring this page?'* Record, per page, the first write number (1-6) where your")
A("answer becomes yes and stays yes — or 'never'.")
A("")
for widx in range(6):
    for page in F20_ORDER:
        uid = f"{page}-u{widx:02d}"
        u = units[uid]
        A(f"### {F20_ALIAS[page]} — write {widx+1}")
        A("")
        A(f"> {scrub(u.text.strip(), F20_ALIAS)}")
        A("")
        meta = [f"category `{u.raw['category']}`", f"anchor `{u.raw['anchor']}`"]
        ls = links_for(uid)
        if ls:
            shown = ", ".join(f"{d} {anon(t, page)}" for d, t in ls)
            meta.append(f"links: {shown}")
        A("*" + "; ".join(meta) + "*")
        A("")
A("---")
A("")
A("## Part 2 — a name recurring across sources (3 referents)")
A("")
A("Each referent below appears in successive captured sources. After each occurrence,")
A("ask: *'Would I now want the system to propose holding this as its own entity page?'*")
A("Record the first source number where yes — or 'never'. The note under each referent")
A("describes what the occurrences carry.")
A("")
c21 = nn.f21_corpus(surfaced=False)
u21 = {i.id: i for i in c21.items}
for ref in F21_ORDER:
    container = u21[ref]
    A(f"### {F21_ALIAS[ref]}")
    A("")
    A(f"*Occurrence pattern:* {container.text.strip()}")
    A("")
    for i in range(int(container.raw["source_count"])):
        src = u21[f"{ref}-src{i:02d}"]
        A(f"- Source {i+1}: {scrub(src.text.strip(), F21_ALIAS)[:120]}…")
    A("")
A("---")
A("")
A("## Part 3 — quiet window after a restructure")
A("")
A("A page you owned was just split, with your confirmation, into two child pages")
A("(child-a, child-b). The split is done and correct. Maintenance passes now run on a")
A("schedule. Question: *for how many maintenance passes after the split should the")
A("system stay COMPLETELY quiet about merging those children back* — i.e. at which")
A("pass number would a merge-back proposal stop feeling like the system second-guessing")
A("a decision it just executed, and start being welcome if the children really did end")
A("up near-duplicates? Answer with that pass number (1 = a proposal already next pass")
A("is fine), or 'never' (merge-back proposals only on my explicit request).")
A("")
A("---")
A("")
A("## Part 4 — false-positive budget (one question)")
A("")
A("Of every 10 restructure/entity proposals the system surfaces, how many may turn out")
A("to be ones you dismiss as irrelevant before the feature overall feels like a nag you")
A("would turn off? Answer 0-10.")
A("")
A("---")
A("")
A("## ANSWER BLOCK — fill in and send back")
A("")
A("```")
for page in F20_ORDER:
    A(f"{F20_ALIAS[page]}: write __   (1-6 or never)")
for ref in F21_ORDER:
    A(f"{F21_ALIAS[ref]}: source __   (1-3 or never)")
A("Quiet window: __ passes   (number or never)")
A("FP budget: __ / 10")
A("```")
OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

key = ["# Alias key (kept out of the annotator sheet)"]
key += [f"{F20_ALIAS[p]} = {p} ({nn.F20_PAGES[p][1]}; clusters={nn.F20_PAGES[p][0]})" for p in F20_ORDER]
key += [f"{F21_ALIAS[r]} = {r}" for r in F21_ORDER]
KEY.write_text("\n".join(key) + "\n", encoding="utf-8")
print(f"sheet: {OUT}")
print(f"key: {KEY}")
