---
exomem_id: dbba3b6c-45d3-578e-94ed-422485a1239d
type: research-note
project: fernwatch
status: active
created: 2026-06-20
updated: 2026-07-30
sources: []
tags: [software, charts, frontend]
---

# Dashboard charting options

## Question

Which charting approach suits a small dashboard of soil-moisture and temperature lines with a few thousand points each?

## Findings

- A canvas-based library draws thousands of points smoothly and keeps the bundle small enough.
- Hand-written SVG looked crisp but got slow past a couple of thousand points per line.
- Server-rendered images would be simplest, but the dashboard needs hover values.

The shortlist ended with one canvas library, which is now in use.

## Relations

- about_entity [[Entities/Libraries/Chart.js]]
- relates_to [[Entities/Concepts/Fernwatch]]
