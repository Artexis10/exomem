## Why

An owner who moves an existing Evermatter vault to Cloud loses its speech results. `exomem cloud import` sends the folder as an `adopt` import. Adoption files each recording as a new artifact with a pending sidecar. It files the old completed sidecar beside the recording as an unbound text Source. The cell then transcribes the recording again, and the speaker labels are lost. The client also leaves hidden entries behind by default, so the part pages of a long transcript do not reach the cell.

A whole-vault restore keeps the sidecars byte-identical, and the cell skips them. A restore must fit the cell's volume, however. Only adopt imports can arrive in windows, so a vault larger than the volume must go through adoption.

Transcribing again on the cell is slow, and it cannot bring the labels back. Cell Whisper runs at about 0.43 times real time, and a cell cannot diarise at all.

## What Changes

- **Adoption keeps an Evermatter speech result.** Adoption pairs an audio or video original with the page beside it, when that page holds a completed extraction for the same bytes. The original's Source keeps the transcript, the timed text, the speaker fields and the part pages. The cell does not transcribe the recording again. A page that arrives in a later window pairs with its stored original. It replaces that Source's result under the same rule as an attach: a pending, failed or unlabelled result gives way, and a labelled or named one stays, with the page filed beside it and linked. A page that does not match keeps today's behaviour.
- **Imports carry part folders.** `exomem import` and `exomem cloud import` select the part folder of each selected page. A windowed import keeps each page with its part folder.
- **New audio is transcribed on the cell's CPU.** This change adds no mechanism for it. The owner's cell gets the speech engine through the existing per-cell engine selection, under an operator-approved live change.
- **The owner's agent attaches speech results.** A new governed operation, `media.attach-speech`, lets an agent attach a transcript, speaker turns, track names and speaker names to an existing media Source. The owner's agent computes them on the owner's machine with the personal install, over its existing connection. The result binds to the original's SHA-256 and size. The cell renders and publishes it as it does its own results. A platform burst GPU job stays a priced later alternative that would call the same operation.
- **Multitrack recordings.** Audio streams and channels declared as separate microphones, and separate files grouped as one session, are transcribed per track. Without a declaration, speech reads the first audio stream, as today. Turns carry their track's name. The fields come from published metadata specifications.
- **Naming speakers after the fact.** An agent can rename a recording's speaker labels. The names survive a reprocess and an attach. Keeping a voice for later recordings stays an owner action on a personal install, never an agent tool.

This change replaces an earlier draft that specified a speech worker on the owner's machine. That draft's capability, routes, credential scope, proof purpose, command and change to `cloud-multimodal-processing` are withdrawn. No speech worker, route, credential or service is added. A result computed outside the cell reaches it only through `media.attach-speech`.

## Capabilities

### New Capabilities
- `speech-result-attachment`: the agent operation that attaches a speech result to an existing media Source, bound by the original's SHA-256.
- `multitrack-speech`: per-track transcription, track names from published metadata, crosstalk handling, session grouping and diarisation within a track.

### Modified Capabilities
- `adoption-studio`: adoption keeps a completed Evermatter speech result for its original, also across windows, and import selection carries part folders.
- `cloud-vault-import`: a windowed Cloud import keeps each page with its part folder.
- `speaker-diarization`: renaming a recording's speaker labels in place after the fact, and keeping a voice as an owner action.

## Impact

- Adoption engine: pairing in the plan, the kept Source write in apply, pairing with a stored original, plan and apply counts, and the extraction request for a kept result.
- Import selection: part folders in the selection that local and Cloud imports share. Window order in the Cloud CLI.
- Media operations: `media.attach-speech` on the write door, sharing the in-cell speaker assignment, transcript rendering and extraction publication. Agent guidance for it.
- Media worker: per-track transcription, per-track progress and timeouts, and the crosstalk rule. The rule's margin lives in the speech decoding policy data.
- Note-type registry: one shipped entry for session transcripts.
- Speaker naming: the rename operation, name carry-over on reprocess and attach, and the `enroll-speaker` form that takes a recording and a label.
- Operations: one live change to switch on the speech engine for the owner's cell.
