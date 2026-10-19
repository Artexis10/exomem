---
type: tracker
tracks: x3
started: 2024-01-12
rep_counting_started: 2024-02-05
created: 2024-09-20
updated: 2024-11-19
---
# X3 Training Log

This is an invented training log for Records acceptance tests. Its dates, bands,
counts, notes, and historical entries describe no person's training.

> **New session:** insert **"X3 Push"** or **"X3 Pull"** from the template folder at the top of *Sessions*. Fill in the date, bands, and rep counts.
> Line format: `Movement | band | reps`. Notation is in the **Legend**.
> The invented undated archive lives in [[Records/Health/X3/Historical Reps (undated)]].

## Sessions (newest first)

### 2024-11-19 · Push
- Overhead Press | blue short | 12
- Split Squat L | green short | 19
- Chest Press | green short | 14+4
- Tricep Press | blue short | 17
- Split Squat R | green short | 20

### 2024-10-14 · Push (Stopped early)
- Overhead Press | blue short | 6
- Split Squat L | green short | 
- Chest Press | green short | 
- Tricep Press | blue short | 
- Split Squat R | green short | 

### 2024-10-13 · Pull
- Deadlift | green short | 24
- Bent Row | blue short | 13
- Bicep Curl | blue short | 10?
- Calf Raise | green short | 32

### 2024-10-02 · Push
- Overhead Press | blue short | 11
- Split Squat L | green short | 18
- Chest Press | green short | 16
- Tricep Press | blue short | 14
- Split Squat R | green short | 19

### 2024-09-28 · Pull
- Deadlift | green short | 22
- Bent Row | blue short | 12
- Bicep Curl | blue short | 9
- Calf Raise | green short | 28

### 2024-09-27 · Push
- Overhead Press | blue short | 10
- Split Squat L | green short | 16
- Chest Press | green short | 13
- Tricep Press | blue short | 12
- Split Squat R | green short | 17

## Current bands (what the example templates prefill)

- **Push:** Overhead Press and Tricep Press use `blue short`; the other movements use `green short`.
- **Pull:** Bent Row and Bicep Curl use `blue short`; Deadlift and Calf Raise use `green short`.

## Legend

**Notation**
- `14+4` — 14 reps, then a rest-pause for four more.
- `!` — continuous reps after `!` in the historical archive.
- `?` — an uncertain rep count.
- `short` and `long` — example band lengths.
- `green ×2` or `green + blue` in the band field — stacked bands. A `+` in the reps field means rest-pause.
- A blank rep field stays blank when a session stops early.

**Session structure**
- **Push:** Overhead Press → Split Squat L → Chest Press → Tricep Press → Split Squat R.
- **Pull:** Deadlift → Bent Row → Bicep Curl → Calf Raise.

The labels and notation remain ordinary Markdown data. The collection schema
defines how Records reads the rows; it does not judge training progress.
