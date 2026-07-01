# Design — Zero-shot image tags

## Context

exomem already loads a CLIP model (`embeddings.get_clip_model()`, `clip-ViT-B-32`) that encodes
images (`embed_image`) and text queries (`embed_clip_text`) into one shared 512-dim space, used
today for visual `find`. Image extraction (`extract._ocr_image`) produces the text that the
sidecar stores and that BM25 + bge index. That text is currently OCR output plus an optional
default-off caption. The depicted content of a textless photo never reaches the text index.

A zero-shot classifier falls out of CLIP for free: embed a fixed tag vocabulary with the SAME
model's text encoder, cosine an image vector against it, and the high-scoring tags name what the
image shows. This change wires that into the extraction seam so the tags are indexed like any
other extracted text.

## Goals / non-goals

- **Goal:** an image's depicted concepts (`invoice`, `whiteboard`, `screenshot`) become plain
  words in the indexed sidecar text, BM25-findable and bge-embedded — no special `find` path.
- **Goal:** reuse the loaded CLIP model + its device logic; add no dependency; stay default-off,
  soft-fail, and byte-identical when unused.
- **Non-goal:** video-keyframe tagging, a structured `tags:` field or `tag:` filter, custom/
  per-vault vocabularies, prompt-templated vocab, re-tagging already-indexed images. (Future.)

## Pure-substrate justification

CLIP image and text encoders are frozen, deterministic functions (pixels→vector, words→vector) —
the same class as Tesseract OCR, the bge embedder, and the existing CLIP visual search. Tagging is
a fixed cosine comparison against frozen vocabulary vectors with a fixed threshold: a
*measurement*, not a judgment, and not a generative or reasoning model. The vocabulary is a fixed
human-authored list (a one-time brain act); the server only measures "this image is within τ of
the `invoice` text vector." Unlike captioning (which *generates* language and therefore ships
off), tagging only SELECTS from a frozen list — but it still ships default-off for parity and so
the threshold can be tuned on real images before it changes any indexed text. In-bounds.

## Architecture

Per image, when `KB_MCP_IMAGE_TAGS` is set:

1. **Embed the vocabulary once** (new `image_tags._vocab_matrix`, cached per process): the ~190
   fixed terms in `image_tags.TAG_VOCAB` → `(N, 512)` via a new batched
   `embeddings.embed_clip_texts()` (CLIP's text encoder, L2-normalized). One encode per process.
2. **Embed the image** (existing `embeddings.embed_image`): the CLIP image vector (512, normalized).
3. **Score + select** (`image_tags.compute_tags`): `scores = vocab_matrix @ image_vec`; take the
   top-K indices by descending cosine whose score ≥ threshold → tag strings.
4. **Append at the seam** (`extract._maybe_image_tags`, called by `_ocr_image` after
   `_maybe_caption`): render `Tags: a, b, c` and append it to the extracted text
   (`<text>\n\nTags: …`, or just the line when OCR/caption text is empty); suffix the engine with
   `+tags` for provenance. The text then flows through the unchanged sidecar/index path.

Defaults: `top_k=5`, `threshold=0.22` (raw cosine). Both overridable via `KB_MCP_IMAGE_TAGS_TOPK`
/ `KB_MCP_IMAGE_TAGS_THRESHOLD`.

## Decisions

- **Append (not prepend) one line.** OCR/caption text is the primary signal; tags are secondary
  metadata, so they go after. A single `Tags:` line keeps the addition obvious and easy to strip
  if ever undesired, and reads naturally in the sidecar.
- **Vocabulary is a generic Python constant in `image_tags.TAG_VOCAB`, not a data file.** ~190
  lowercase single concepts (document/screen kinds, objects, scenes, nature, people, animals,
  food, vehicles, art). It ships under `src/kb_mcp/` and is leak-guarded
  (`tests/test_scaffold_no_leak.py` scans all of `src/kb_mcp/`), so it is deliberately brand- and
  identity-free. A constant keeps it import-time-cheap and unit-testable without resource loading.
- **Raw-cosine threshold, not softmax.** Multiple independent tags can apply to one image, so an
  absolute per-tag cosine floor is the right gate (softmax-over-vocab would force a single winner
  and distort multi-concept images). Raw CLIP cosines are low-magnitude; 0.22 is a sane default,
  tuned desk-side on real images.
- **Raw vocab words, no prompt template.** Terms are embedded verbatim ("invoice"), not as
  "a photo of an invoice". Simpler and deterministic; prompt-templating is a noted desk-side
  refinement if recall needs it, behind the same threshold knob.
- **Reuse CLIP device logic.** No new device function: tagging goes through `embed_image` /
  `embed_clip_texts`, which use `embeddings.get_clip_model()` → `_clip_device()` (CPU when ASR is
  active, the existing whisper-cuDNN-shadow fix; `KB_MCP_CLIP_DEVICE` override).
- **Compute at the extraction seam, accept a second image encode.** `_ocr_image` (the do_ocr job)
  encodes the image for tags; the separate do_clip job encodes it again for `ClipIndex`. Two
  forward passes through one shared, already-warm ViT-B/32 — sub-second, off the request path.
  Keeping tagging at the extraction seam (where the indexed text is assembled) is cleaner than
  threading a cached vector across two independent worker jobs.

## Soft-fail & default-off

`extract._maybe_image_tags` returns the input `(text, engine)` unchanged when the flag is unset —
so with `KB_MCP_IMAGE_TAGS` off, `image_tags`/`embeddings` aren't even imported and output is
byte-identical. When on, `image_tags.compute_tags` never raises: a missing dep/Pillow/model
(`ClipUnavailable`), an unreadable image, or any encode error returns `[]` → no tags appended,
extraction completes normally. The vocab matrix is not cached on failure, so a transient absence
can recover on a later image.

## Risks

- Threshold tuning is empirical: too low → noisy/wrong tags pollute the index; too high → no tags.
  Mitigated by default-off + a desk-side smoke on real images before enabling, and the env knob.
- A second CLIP image encode per image (tags + ClipIndex) — acceptable (tiny model, off the
  request path); a shared-vector optimization is a noted follow-up, not done here.
- Only newly-extracted images get tags; existing indexed images are unchanged until re-extracted.
  A `backfill-media` re-tag pass is out of scope here.
