# Judge–human agreement — blind labelling sheet

50 items. For each one, answer exactly this question:

> **Does the candidate response convey the expected answer?**

## How to fill this in

Every item ends with a line that looks like this:

```
**Your label:** <yes | no | unsure>
```

Replace the whole `<yes | no | unsure>` placeholder with one word.
Nothing else on the line, no quotes, no punctuation. Leave the rest of
the item untouched. Capitalisation does not matter.

Use `unsure` only when the item is genuinely ambiguous. Unsure rows are
excluded from the agreement statistic and reported separately as a
count — they cost nothing, but they do not help either.

If you would rather not scroll this file, there is a one-line-per-item
form next to it (`…-answers.md`). Fill in either; both are accepted.

## Two worked examples

These two are **examples only** — they are not in the sheet and are not
scored. Real items start at J001.

### Example A → `yes`

**Question:** What is the current review cadence for Project Sablereach?

**Expected answer:** every Tuesday

**Candidate response:**

```
# Project Sablereach budget amendment

Only the budget clause of the Project Sablereach steering decision changes.
The pilot budget for Project Sablereach is now 51000 credits.
Reviews for Project Sablereach stay on every Tuesday, unchanged.

[ctx:1]
```

**Your label:** yes

Why: the text states the cadence, *every Tuesday*, even though it is a
raw note about the budget and never phrases it as an answer. Content is
what counts, not presentation.

### Example B → `no`

**Question:** What is the current delivery deadline for Project Quarrypoint?

**Expected answer:** 2025-03-28

**Candidate response:**

```
# Project Quarrypoint kickoff brief

Arden Labs approved the kickoff of Project Quarrypoint.
The delivery deadline for Project Quarrypoint is 2025-03-14.

[ctx:1]
```

**Your label:** no

Why: it looks authoritative and is clearly about the right project, but
the date is the superseded one. A confident wrong answer is still `no`.

## What to resist

- Do **not** reward a fluent, well-structured response that never states
  the fact.
- Do **not** punish an ugly document dump that does state it.
- Do **not** try to guess what the automated judge would say. The point
  is to find out where it disagrees with you.

The judge's verdict and every deterministic gate result are deliberately
absent from this file. If you find you can infer them anyway, say so —
that is itself a finding about the sheet.

Candidate responses are shown in full and exactly as the judge receives
them: source references appear as neutral `[ctx:N]` tokens and product
names are replaced, so neither of you can tell which system answered.
Retrieval-mode contenders return document text rather than prose.

---

## J001  (1 of 50)

**Question:** What is the current review cadence for Project Sablereach?

**Expected answer:** every Tuesday

**Candidate response:**

```
---
type: source
[system]_id: 6df0e9de-112e-43c1-98dc-dedb8428ed3a
title: Project Sablereach budget amendment
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Sablereach budget amendment

## Capture

# Project Sablereach budget amendment

Only the budget clause of the Project Sablereach steering decision changes.

The pilot budget for Project Sablereach is now 51000 credits.

Reviews for Project Sablereach stay on every Tuesday, unchanged.

[ctx:1]


---
type: source
[system]_id: b4bb8d57-9410-4b04-abcc-45dc4cd2985f
title: Project Sablereach steering decision
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Sablereach steering decision

## Capture

# Project Sablereach steering decision

Decision for Project Sablereach, clause one: reviews happen every Tuesday.

Decision for Project Sablereach, clause two: the pilot budget is 48000 credits.

[ctx:2]


---
type: source
[system]_id: e4995b0f-b16d-452d-9067-988bb2f8551d
title: Project Cindergate hosting decision
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Cindergate hosting decision

## Capture

# Project Cindergate hosting decision

After a full review, Project Cindergate will run on the Petra Group platform.

The hosting provider for Project Cindergate is Petra Group.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J002  (2 of 50)

**Question:** What is the confirmed standing of the Project Hollowbank pilot?

**Expected answer:** approved for the northern site

**Candidate response:**

```
---
type: source
[system]_id: 3a19ffeb-efd2-4e39-acbe-3b8673f58e67
title: Project Hollowbank pilot sign-off
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Hollowbank pilot sign-off

## Capture

# Project Hollowbank pilot sign-off

Sign-off complete: the Project Hollowbank pilot is approved for the northern site.

[ctx:1]


---
type: source
[system]_id: 4edba59c-14eb-44c2-b674-503253caabb2
title: Project Hollowbank procurement statement
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Hollowbank procurement statement

## Capture

# Project Hollowbank procurement statement

Procurement confirmed Project Hollowbank keeps its full supplier roster.

The single-supplier story is wrong.

[ctx:2]


---
type: source
[system]_id: c20f8de5-16e8-4d74-bca1-65e239462171
title: Project Cinderspur budget amendment
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Cinderspur budget amendment

## Capture

# Project Cinderspur budget amendment

Only the budget clause of the Project Cinderspur steering decision changes.

The pilot budget for Project Cinderspur is now 78000 credits.

Reviews for Project Cinderspur stay on every Tuesday, unchanged.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J003  (3 of 50)

**Question:** Thanks, that looks good - go ahead.

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
(empty response)
```

**Your label:** <yes | no | unsure>

---

## J004  (4 of 50)

**Question:** On what date did Project Cinderfall actually complete its launch?

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J005  (5 of 50)

**Question:** What does the wall chart show for the current load floor of Project Larkcrest?

**Expected answer:** 63

**Candidate response:**

```
---
type: source
[system]_id: c1565771-ed68-4a8a-bdc5-41e47cc4d157
title: Project Larkcrest chart note
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Larkcrest chart note

## Capture

# Project Larkcrest chart note

A fresh wall chart for Project Larkcrest is posted by the stairwell.

Last month the load floor for Project Larkcrest stood at 57 points.

[ctx:1]
```

**Your label:** <yes | no | unsure>

---

## J006  (6 of 50)

**Question:** Thanks, that looks good - go ahead.

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
(empty response)
```

**Your label:** <yes | no | unsure>

---

## J007  (7 of 50)

**Question:** What fine amount did the Project Sablegate quarterly audit assess?

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J008  (8 of 50)

**Question:** As of week 3, what initiative had Lumo Institute announced?

**Expected answer:** expansion into the Barstead market

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J009  (9 of 50)

**Question:** Thanks, that looks good - go ahead.

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
(empty response)
```

**Your label:** <yes | no | unsure>

---

## J010  (10 of 50)

**Question:** Were any critical findings reported in the Project Mossfield quarterly audit?

**Expected answer:** none found

**Candidate response:**

```
---
type: source
[system]_id: f106ec67-7d7f-4f47-99f3-1c95582b4be2
title: Project Mossfield quarterly audit
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Mossfield quarterly audit

## Capture

# Project Mossfield quarterly audit

The quarterly audit for Project Mossfield covered 31 services end to end.

Critical findings for Project Mossfield this quarter: none found.

[ctx:1]


---
type: source
[system]_id: 7b85558d-b03e-4cee-a9b2-99501f698085
title: Project Sablefall quarterly audit
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Sablefall quarterly audit

## Capture

# Project Sablefall quarterly audit

The quarterly audit for Project Sablefall covered 9 services end to end.

Critical findings for Project Sablefall this quarter: none found.

[ctx:2]


---
type: source
[system]_id: ae10b9a6-7bde-4d52-8302-e7d4a391dc84
title: Project Sablegate quarterly audit
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Sablegate quarterly audit

## Capture

# Project Sablegate quarterly audit

The quarterly audit for Project Sablegate covered 39 services end to end.

Critical findings for Project Sablegate this quarter: none found.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J011  (11 of 50)

**Question:** What was the pilot budget for Project Cinderspur as of week 3, before the amendment?

**Expected answer:** 73000

**Candidate response:**

```
---
type: source
[system]_id: c20f8de5-16e8-4d74-bca1-65e239462171
title: Project Cinderspur budget amendment
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Cinderspur budget amendment

## Capture

# Project Cinderspur budget amendment

Only the budget clause of the Project Cinderspur steering decision changes.

The pilot budget for Project Cinderspur is now 78000 credits.

Reviews for Project Cinderspur stay on every Tuesday, unchanged.

[ctx:1]


---
type: source
[system]_id: 92ff87f6-0ec8-4446-bca9-1378041981c6
title: Project Mossrun budget amendment
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Mossrun budget amendment

## Capture

# Project Mossrun budget amendment

Only the budget clause of the Project Mossrun steering decision changes.

The pilot budget for Project Mossrun is now 76000 credits.

Reviews for Project Mossrun stay on every Tuesday, unchanged.

[ctx:2]


---
type: source
[system]_id: 892383b6-7168-4f8e-9f9b-8427d907b619
title: Project Quarryreach budget amendment
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Quarryreach budget amendment

## Capture

# Project Quarryreach budget amendment

Only the budget clause of the Project Quarryreach steering decision changes.

The pilot budget for Project Quarryreach is now 77000 credits.

Reviews for Project Quarryreach stay on every Tuesday, unchanged.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J012  (12 of 50)

**Question:** Thanks, that looks good - go ahead.

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
(empty response)
```

**Your label:** <yes | no | unsure>

---

## J013  (13 of 50)

**Question:** What was the weekly flux index reading for Project Emberspur as of week 5?

**Expected answer:** 197

**Candidate response:**

```
---
type: source
[system]_id: acb37f90-1ef8-49c0-b153-8a9ebd279b66
title: Project Emberspur week 5 check
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Emberspur week 5 check

## Capture

# Project Emberspur week 5 check

Weekly check: the flux index for Project Emberspur measured 197.

[ctx:1]


---
type: source
[system]_id: d4379dc8-94d0-4348-9506-b76e24e8c5e2
title: Project Emberspur week 6 check
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Emberspur week 6 check

## Capture

# Project Emberspur week 6 check

Weekly check: the flux index for Project Emberspur measured 209.

[ctx:2]


---
type: source
[system]_id: 3b85c653-3d06-4190-9526-909b01c34e4c
title: Project Emberspur week 7 check
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Emberspur week 7 check

## Capture

# Project Emberspur week 7 check

Weekly check: the flux index for Project Emberspur measured 217.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J014  (14 of 50)

**Question:** When is the Project Mossspur demo scheduled?

**Expected answer:** 2025-03-14 / 2025-03-18

**Candidate response:**

```
---
type: source
[system]_id: 81a232e9-1304-46be-aa83-9052449003c5
title: Project Mossspur planning note by Renric Ostdal
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Mossspur planning note by Renric Ostdal

## Capture

# Project Mossspur planning note by Renric Ostdal

Renric Ostdal recorded the Project Mossspur demo date as 2025-03-14.

Renric Ostdal booked the orchard hall for the Project Mossspur demo.

[ctx:1]


---
type: source
[system]_id: de097ed8-b6fd-4750-8979-5197de57e6ef
title: Project Mossspur planning note by Salen Mareth
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Mossspur planning note by Salen Mareth

## Capture

# Project Mossspur planning note by Salen Mareth

Salen Mareth recorded the Project Mossspur demo date as 2025-03-18.

[ctx:2]
```

**Your label:** <yes | no | unsure>

---

## J015  (15 of 50)

**Question:** How many grams did the Project Emberspur sample shipment weigh?

**Expected answer:** 4400 / 4.4

**Candidate response:**

```
---
type: source
[system]_id: 14f008f5-1012-4611-8011-d480e6fad03d
title: Project Emberspur shipment record
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Emberspur shipment record

## Capture

# Project Emberspur shipment record

The Project Emberspur sample shipment weighed 4.4 kg on the dock scale.

[ctx:1]
```

**Your label:** <yes | no | unsure>

---

## J016  (16 of 50)

**Question:** How many advisories stand against Ulmport Depot from its inspection?

**Expected answer:** 3

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J017  (17 of 50)

**Question:** What annual compensation is set for Nela Velwick?

**Expected answer:** 146000

**Candidate response:**

```
---
type: source
[system]_id: 895ca666-67a8-4842-b8c0-4c6b43fc8604
title: Compensation memo for Nela Velwick
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Compensation memo for Nela Velwick

## Capture

# Compensation memo for Nela Velwick

Annual compensation for Nela Velwick is set at 146000 credits.

[ctx:1]


---
type: source
[system]_id: 363cdabf-6285-44d9-8df2-a1473f5e9c95
title: Compensation memo for Istala Feneth
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Compensation memo for Istala Feneth

## Capture

# Compensation memo for Istala Feneth

Annual compensation for Istala Feneth is set at 88000 credits.

[ctx:2]


---
type: source
[system]_id: 05a33191-f6ca-4472-aaf2-7cd1c5f816d2
title: Compensation memo for Renric Ostuna
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Compensation memo for Renric Ostuna

## Capture

# Compensation memo for Renric Ostuna

Annual compensation for Renric Ostuna is set at 129000 credits.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J018  (18 of 50)

**Question:** What is the latest weekly drift rate reading for Project Drifthollow?

**Expected answer:** 356

**Candidate response:**

```
---
type: source
[system]_id: 2ac1efb6-f484-4aa7-8dd9-578f5b677a18
title: Project Drifthollow chart note
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Drifthollow chart note

## Capture

# Project Drifthollow chart note

A fresh wall chart for Project Drifthollow is posted by the stairwell.

Last month the drift ceiling for Project Drifthollow stood at 65 points.

[ctx:1]


---
type: source
[system]_id: b75da689-eb0b-470a-90ad-22d059284f30
title: Project Drifthollow week 5 check
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Drifthollow week 5 check

## Capture

# Project Drifthollow week 5 check

Weekly check: the drift rate for Project Drifthollow measured 318.

[ctx:2]


---
type: source
[system]_id: 1dd25ed7-c291-440a-9069-93c1beac15c5
title: Project Drifthollow week 6 check
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Drifthollow week 6 check

## Capture

# Project Drifthollow week 6 check

Weekly check: the drift rate for Project Drifthollow measured 341.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J019  (19 of 50)

**Question:** What partner discount does Norva Dynamics currently offer?

**Expected answer:** 29

**Candidate response:**

```
---
type: source
[system]_id: ee3ce6b8-f6ad-462f-989e-683fd6ad576a
title: Norva Dynamics partner offer
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Norva Dynamics partner offer

## Capture

# Norva Dynamics partner offer

Norva Dynamics extends a partner discount of 29 percent.

The offer stands until 2025-03-03 and lapses after that date.

[ctx:1]
```

**Your label:** <yes | no | unsure>

---

## J020  (20 of 50)

**Question:** Now that the embargo lapsed, what did the Vanta Partners board decide?

**Expected answer:** approved the granary initiative

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J021  (21 of 50)

**Question:** How many grams did the Project Larkcrest sample shipment weigh?

**Expected answer:** 4100 / 4.1

**Candidate response:**

```
---
type: source
[system]_id: d6536680-0808-4cac-aabe-a482049c121e
title: Project Larkcrest shipment record
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Larkcrest shipment record

## Capture

# Project Larkcrest shipment record

The Project Larkcrest sample shipment weighed 4.1 kg on the dock scale.

[ctx:1]
```

**Your label:** <yes | no | unsure>

---

## J022  (22 of 50)

**Question:** What is the latest weekly churn window reading for Project Larkcrest?

**Expected answer:** 273

**Candidate response:**

```
---
type: source
[system]_id: d9f65fa5-e114-47ee-94ef-be45b5e978bf
title: Project Larkcrest week 5 check
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Larkcrest week 5 check

## Capture

# Project Larkcrest week 5 check

Weekly check: the churn window for Project Larkcrest measured 226.

[ctx:1]


---
type: source
[system]_id: 2dc6c241-a21c-4aa6-8294-f245ef5a843f
title: Project Larkcrest week 6 check
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Larkcrest week 6 check

## Capture

# Project Larkcrest week 6 check

Weekly check: the churn window for Project Larkcrest measured 247.

[ctx:2]


---
type: source
[system]_id: 26b94cf2-ed99-4d07-87ed-399d14dacd19
title: Project Larkcrest week 7 check
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Larkcrest week 7 check

## Capture

# Project Larkcrest week 7 check

Weekly check: the churn window for Project Larkcrest measured 273.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J023  (23 of 50)

**Question:** How many grams did the Project Emberfall sample shipment weigh?

**Expected answer:** 4300 / 4.3

**Candidate response:**

```
---
type: source
[system]_id: 4d72a69d-808f-4bb9-9f7e-796e857312c0
title: Project Emberfall shipment record
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Emberfall shipment record

## Capture

# Project Emberfall shipment record

The Project Emberfall sample shipment weighed 4.3 kg on the dock scale.

[ctx:1]
```

**Your label:** <yes | no | unsure>

---

## J024  (24 of 50)

**Question:** What was the pilot budget for Project Quarryreach as of week 3, before the amendment?

**Expected answer:** 73000

**Candidate response:**

```
---
type: source
[system]_id: c20f8de5-16e8-4d74-bca1-65e239462171
title: Project Cinderspur budget amendment
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Cinderspur budget amendment

## Capture

# Project Cinderspur budget amendment

Only the budget clause of the Project Cinderspur steering decision changes.

The pilot budget for Project Cinderspur is now 78000 credits.

Reviews for Project Cinderspur stay on every Tuesday, unchanged.

[ctx:1]


---
type: source
[system]_id: 892383b6-7168-4f8e-9f9b-8427d907b619
title: Project Quarryreach budget amendment
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Quarryreach budget amendment

## Capture

# Project Quarryreach budget amendment

Only the budget clause of the Project Quarryreach steering decision changes.

The pilot budget for Project Quarryreach is now 77000 credits.

Reviews for Project Quarryreach stay on every Tuesday, unchanged.

[ctx:2]


---
type: source
[system]_id: 92ff87f6-0ec8-4446-bca9-1378041981c6
title: Project Mossrun budget amendment
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Mossrun budget amendment

## Capture

# Project Mossrun budget amendment

Only the budget clause of the Project Mossrun steering decision changes.

The pilot budget for Project Mossrun is now 76000 credits.

Reviews for Project Mossrun stay on every Tuesday, unchanged.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J025  (25 of 50)

**Question:** How many services did the Project Sablefall quarterly audit cover?

**Expected answer:** 9

**Candidate response:**

```
---
type: source
[system]_id: 7b85558d-b03e-4cee-a9b2-99501f698085
title: Project Sablefall quarterly audit
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Sablefall quarterly audit

## Capture

# Project Sablefall quarterly audit

The quarterly audit for Project Sablefall covered 9 services end to end.

Critical findings for Project Sablefall this quarter: none found.

[ctx:1]


---
type: source
[system]_id: f106ec67-7d7f-4f47-99f3-1c95582b4be2
title: Project Mossfield quarterly audit
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Mossfield quarterly audit

## Capture

# Project Mossfield quarterly audit

The quarterly audit for Project Mossfield covered 31 services end to end.

Critical findings for Project Mossfield this quarter: none found.

[ctx:2]


---
type: source
[system]_id: ae10b9a6-7bde-4d52-8302-e7d4a391dc84
title: Project Sablegate quarterly audit
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Sablegate quarterly audit

## Capture

# Project Sablegate quarterly audit

The quarterly audit for Project Sablegate covered 39 services end to end.

Critical findings for Project Sablegate this quarter: none found.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J026  (26 of 50)

**Question:** What did the yield floor for Project Drifthollow read as of week 4?

**Expected answer:** 439

**Candidate response:**

```
---
type: source
[system]_id: f7b53e63-e188-48d5-9066-871017c93183
title: Project Drifthollow status digest refresh
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Drifthollow status digest refresh

## Capture

# Project Drifthollow status digest refresh

The refreshed digest replaces the earlier edition.

The yield floor for Project Drifthollow now reads 467.

[ctx:1]
```

**Your label:** <yes | no | unsure>

---

## J027  (27 of 50)

**Question:** Given everything now known, what was the valid bench readout for Project Larkpoint as of week 2?

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J028  (28 of 50)

**Question:** Now that the embargo lapsed, what did the Arden Institute board decide?

**Expected answer:** approved the harbor initiative

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J029  (29 of 50)

**Question:** Which vendor performed the security audit for Project Quarrypoint?

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J030  (30 of 50)

**Question:** How did the hosting decision for Project Larkrun change over time, and why?

**Expected answer:** Arden Dynamics / Lumo Partners

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J031  (31 of 50)

**Question:** What partner discount does Norva Dynamics offer now that week 8 has passed?

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J032  (32 of 50)

**Question:** As of week 2, how many advisories had the Melmoor Depot inspection logged?

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
---
type: source
[system]_id: a989ea62-ce25-496e-a2e9-b41c8af0cbcd
title: Melmoor Depot inspection report
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Melmoor Depot inspection report

## Capture

# Melmoor Depot inspection report

The site inspection at Melmoor Depot took place in mid-January.

The report reached the archive only weeks after the visit.

The inspection at Melmoor Depot logged 4 advisories.

[ctx:1]
```

**Your label:** <yes | no | unsure>

---

## J033  (33 of 50)

**Question:** As of week 2, how many advisories did the Melmoor Depot inspection log?

**Expected answer:** 4

**Candidate response:**

```
---
type: source
[system]_id: a989ea62-ce25-496e-a2e9-b41c8af0cbcd
title: Melmoor Depot inspection report
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Melmoor Depot inspection report

## Capture

# Melmoor Depot inspection report

The site inspection at Melmoor Depot took place in mid-January.

The report reached the archive only weeks after the visit.

The inspection at Melmoor Depot logged 4 advisories.

[ctx:1]
```

**Your label:** <yes | no | unsure>

---

## J034  (34 of 50)

**Question:** Is Project Hollowgate changing its supplier setup?

**Expected answer:** switching to a single supplier

**Candidate response:**

```
---
type: source
[system]_id: a00f6619-96c0-4faf-9e44-848b27342342
title: Project Hollowgate procurement statement
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Hollowgate procurement statement

## Capture

# Project Hollowgate procurement statement

Procurement confirmed Project Hollowgate keeps its full supplier roster.

The single-supplier story is wrong.

[ctx:1]


---
type: source
[system]_id: 3cee3b93-404f-473f-a66c-7fd4965c745b
title: Project Hollowgate supplier chatter
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Hollowgate supplier chatter

## Capture

# Project Hollowgate supplier chatter

Chatter suggests Project Hollowgate is switching to a single supplier next quarter.

[ctx:2]


---
type: source
[system]_id: ba574eda-bebe-407e-b108-a2b0c4229b9a
title: Project Cinderbank procurement statement
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Cinderbank procurement statement

## Capture

# Project Cinderbank procurement statement

Procurement confirmed Project Cinderbank keeps its full supplier roster.

The single-supplier story is wrong.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J035  (35 of 50)

**Question:** As of week 2, how many advisories did the Ulmport Depot inspection log?

**Expected answer:** 3

**Candidate response:**

```
---
type: source
[system]_id: 1fd70b60-00ef-4e48-bded-12128496ff57
title: Ulmport Depot inspection report
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Ulmport Depot inspection report

## Capture

# Ulmport Depot inspection report

The site inspection at Ulmport Depot took place in mid-January.

The report reached the archive only weeks after the visit.

The inspection at Ulmport Depot logged 3 advisories.

[ctx:1]
```

**Your label:** <yes | no | unsure>

---

## J036  (36 of 50)

**Question:** What initiative had Lumo Partners announced?

**Expected answer:** expansion into the Vorburg market

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J037  (37 of 50)

**Question:** When was the Project Valereach demo scheduled per the first planning note?

**Expected answer:** 2025-03-12

**Candidate response:**

```
---
type: source
[system]_id: 42720a1c-a63e-4d6a-8f70-e4eded032915
title: Project Valereach planning note by Nelen Quinsen
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Valereach planning note by Nelen Quinsen

## Capture

# Project Valereach planning note by Nelen Quinsen

Nelen Quinsen recorded the Project Valereach demo date as 2025-03-12.

Nelen Quinsen booked the foundry hall for the Project Valereach demo.

[ctx:1]


---
type: source
[system]_id: 39d88651-753c-4cfe-9377-adafaef3b1c4
title: Project Valereach planning note by Kaivi Quinuna
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Valereach planning note by Kaivi Quinuna

## Capture

# Project Valereach planning note by Kaivi Quinuna

Kaivi Quinuna recorded the Project Valereach demo date as 2025-03-17.

[ctx:2]
```

**Your label:** <yes | no | unsure>

---

## J038  (38 of 50)

**Question:** As of week 2, how many advisories had the Falford Depot inspection logged?

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
---
type: source
[system]_id: 79b32092-6c9b-49f8-859a-c69c51c80562
title: Falford Depot inspection report
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Falford Depot inspection report

## Capture

# Falford Depot inspection report

The site inspection at Falford Depot took place in mid-January.

The report reached the archive only weeks after the visit.

The inspection at Falford Depot logged 8 advisories.

[ctx:1]
```

**Your label:** <yes | no | unsure>

---

## J039  (39 of 50)

**Question:** What partner discount did Petra Labs offer as of week 4?

**Expected answer:** 18

**Candidate response:**

```
---
type: source
[system]_id: be7ed25c-b271-4c6f-9e52-363cdf301d6e
title: Petra Labs partner offer
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Petra Labs partner offer

## Capture

# Petra Labs partner offer

Petra Labs extends a partner discount of 18 percent.

The offer stands until 2025-03-03 and lapses after that date.

[ctx:1]
```

**Your label:** <yes | no | unsure>

---

## J040  (40 of 50)

**Question:** What is the current delivery deadline for Project Beaconcrest?

**Expected answer:** 2025-03-27

**Candidate response:**

```
---
type: source
[system]_id: 0727fca6-b0a4-48bb-a972-5c39e0c284fe
title: Project Beaconcrest kickoff brief
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Beaconcrest kickoff brief

## Capture

# Project Beaconcrest kickoff brief

Lumo Systems approved the kickoff of Project Beaconcrest.

The delivery deadline for Project Beaconcrest is 2025-03-14.

[ctx:1]


---
type: source
[system]_id: cc3c4e46-a26b-4bba-ab10-159208567505
title: Project Beaconcrest replan memo
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Beaconcrest replan memo

## Capture

# Project Beaconcrest replan memo

The delivery deadline for Project Beaconcrest moved to 2025-03-27.

Scope and ownership are unchanged.

[ctx:2]


---
type: source
[system]_id: f87019cc-6815-41c4-a7e4-d94426b236d8
title: Project Beaconfield kickoff brief
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Beaconfield kickoff brief

## Capture

# Project Beaconfield kickoff brief

Kelva Labs approved the kickoff of Project Beaconfield.

The delivery deadline for Project Beaconfield is 2025-03-13.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J041  (41 of 50)

**Question:** What annual compensation is set for Valen Osteth?

**Expected answer:** 107000

**Candidate response:**

```
---
type: source
[system]_id: 50132cc2-07e2-4c6a-97e7-f4cceeb30afe
title: Compensation memo for Valen Osteth
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Compensation memo for Valen Osteth

## Capture

# Compensation memo for Valen Osteth

Annual compensation for Valen Osteth is set at 107000 credits.

[ctx:1]


---
type: source
[system]_id: 363cdabf-6285-44d9-8df2-a1473f5e9c95
title: Compensation memo for Istala Feneth
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Compensation memo for Istala Feneth

## Capture

# Compensation memo for Istala Feneth

Annual compensation for Istala Feneth is set at 88000 credits.

[ctx:2]


---
type: source
[system]_id: 895ca666-67a8-4842-b8c0-4c6b43fc8604
title: Compensation memo for Nela Velwick
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Compensation memo for Nela Velwick

## Capture

# Compensation memo for Nela Velwick

Annual compensation for Nela Velwick is set at 146000 credits.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J042  (42 of 50)

**Question:** Is Project Cinderbank changing its supplier setup?

**Expected answer:** switching to a single supplier

**Candidate response:**

```
---
type: source
[system]_id: ba574eda-bebe-407e-b108-a2b0c4229b9a
title: Project Cinderbank procurement statement
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Cinderbank procurement statement

## Capture

# Project Cinderbank procurement statement

Procurement confirmed Project Cinderbank keeps its full supplier roster.

The single-supplier story is wrong.

[ctx:1]


---
type: source
[system]_id: 101b7441-9f64-457f-a8e8-c6cd3a682255
title: Project Cinderbank supplier chatter
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Cinderbank supplier chatter

## Capture

# Project Cinderbank supplier chatter

Chatter suggests Project Cinderbank is switching to a single supplier next quarter.

[ctx:2]


---
type: source
[system]_id: 4edba59c-14eb-44c2-b674-503253caabb2
title: Project Hollowbank procurement statement
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Hollowbank procurement statement

## Capture

# Project Hollowbank procurement statement

Procurement confirmed Project Hollowbank keeps its full supplier roster.

The single-supplier story is wrong.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J043  (43 of 50)

**Question:** What initiative does Lumo Institute currently have announced?

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J044  (44 of 50)

**Question:** Is Project Cinderbank moving to a single supplier?

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
---
type: source
[system]_id: 101b7441-9f64-457f-a8e8-c6cd3a682255
title: Project Cinderbank supplier chatter
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Cinderbank supplier chatter

## Capture

# Project Cinderbank supplier chatter

Chatter suggests Project Cinderbank is switching to a single supplier next quarter.

[ctx:1]


---
type: source
[system]_id: ba574eda-bebe-407e-b108-a2b0c4229b9a
title: Project Cinderbank procurement statement
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Cinderbank procurement statement

## Capture

# Project Cinderbank procurement statement

Procurement confirmed Project Cinderbank keeps its full supplier roster.

The single-supplier story is wrong.

[ctx:2]


---
type: source
[system]_id: 514fb625-33ef-46ae-9243-3ccfcfdf3bbc
title: Project Hollowbank supplier chatter
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Hollowbank supplier chatter

## Capture

# Project Hollowbank supplier chatter

Chatter suggests Project Hollowbank is switching to a single supplier next quarter.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J045  (45 of 50)

**Question:** How many services did the Project Mossfield quarterly audit cover?

**Expected answer:** 31

**Candidate response:**

```
---
type: source
[system]_id: f106ec67-7d7f-4f47-99f3-1c95582b4be2
title: Project Mossfield quarterly audit
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Mossfield quarterly audit

## Capture

# Project Mossfield quarterly audit

The quarterly audit for Project Mossfield covered 31 services end to end.

Critical findings for Project Mossfield this quarter: none found.

[ctx:1]


---
type: source
[system]_id: 7b85558d-b03e-4cee-a9b2-99501f698085
title: Project Sablefall quarterly audit
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Sablefall quarterly audit

## Capture

# Project Sablefall quarterly audit

The quarterly audit for Project Sablefall covered 9 services end to end.

Critical findings for Project Sablefall this quarter: none found.

[ctx:2]


---
type: source
[system]_id: ae10b9a6-7bde-4d52-8302-e7d4a391dc84
title: Project Sablegate quarterly audit
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Sablegate quarterly audit

## Capture

# Project Sablegate quarterly audit

The quarterly audit for Project Sablegate covered 39 services end to end.

Critical findings for Project Sablegate this quarter: none found.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J046  (46 of 50)

**Question:** What did the Vanta Dynamics board decide in closed session?

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
---
type: source
[system]_id: 25002173-365f-4065-95ae-7f1fd99a10fe
title: Vanta Dynamics board session digest
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Vanta Dynamics board session digest

## Capture

# Vanta Dynamics board session digest

In closed session, the Vanta Dynamics board approved the ledger initiative.

[ctx:1]


---
type: source
[system]_id: c4a66e1f-b2c1-4a41-970a-c87601ca6ded
title: Vanta Partners board session digest
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Vanta Partners board session digest

## Capture

# Vanta Partners board session digest

In closed session, the Vanta Partners board approved the granary initiative.

[ctx:2]
```

**Your label:** <yes | no | unsure>

---

## J047  (47 of 50)

**Question:** How many points did the flux rate for Project Cinderrun measure in the field report?

**Expected answer:** 47.4

**Candidate response:**

```
---
type: source
[system]_id: e1e8eb06-9f08-4e91-89bf-c874d771e355
title: Project Cinderrun field report
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Cinderrun field report

## Capture

# Project Cinderrun field report

Field check: the flux rate for Project Cinderrun measured 47.4 points.

[ctx:1]


---
type: source
[system]_id: c20c1f9d-e931-4cd3-930c-e5f76868e9f5
title: Project Beaconcrest field report
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Beaconcrest field report

## Capture

# Project Beaconcrest field report

Field check: the recall budget for Project Beaconcrest measured 32.6 points.

[ctx:2]


---
type: source
[system]_id: 3c158870-4245-4969-808c-d13f0ce348e4
title: Project Beaconfield field report
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Beaconfield field report

## Capture

# Project Beaconfield field report

Field check: the uptake score for Project Beaconfield measured 56.3 points.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J048  (48 of 50)

**Question:** How many advisories stand against Vormont Depot from its inspection?

**Expected answer:** 3

**Candidate response:**

```
(the system declined to answer)
```

**Your label:** <yes | no | unsure>

---

## J049  (49 of 50)

**Question:** How many services did the Project Valefield quarterly audit cover?

**Expected answer:** 17

**Candidate response:**

```
---
type: source
[system]_id: b7ef8a9b-80ae-4895-b3db-c5e284a9063e
title: Project Valefield quarterly audit
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Valefield quarterly audit

## Capture

# Project Valefield quarterly audit

The quarterly audit for Project Valefield covered 17 services end to end.

Critical findings for Project Valefield this quarter: none found.

[ctx:1]


---
type: source
[system]_id: f106ec67-7d7f-4f47-99f3-1c95582b4be2
title: Project Mossfield quarterly audit
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Mossfield quarterly audit

## Capture

# Project Mossfield quarterly audit

The quarterly audit for Project Mossfield covered 31 services end to end.

Critical findings for Project Mossfield this quarter: none found.

[ctx:2]


---
type: source
[system]_id: 7b85558d-b03e-4cee-a9b2-99501f698085
title: Project Sablefall quarterly audit
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Project Sablefall quarterly audit

## Capture

# Project Sablefall quarterly audit

The quarterly audit for Project Sablefall covered 9 services end to end.

Critical findings for Project Sablefall this quarter: none found.

[ctx:3]
```

**Your label:** <yes | no | unsure>

---

## J050  (50 of 50)

**Question:** Which technician signed the Meridipoint lab log?

**Expected answer:** (no answer — the corpus does not record this; abstention is correct)

**Candidate response:**

```
---
type: source
[system]_id: 3c33be41-6d55-4081-bb6a-11ded6bfb2d8
title: Meridipoint lab log
source_type: other
captured: 2026-08-01
tags: []
ingested_into: []
---

# Meridipoint lab log

## Capture

# Meridipoint lab log

The Meridipoint instrument recorded 732 units in its first pass.

[ctx:1]
```

**Your label:** <yes | no | unsure>

---
