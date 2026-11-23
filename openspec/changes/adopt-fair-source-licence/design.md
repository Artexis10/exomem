# Design

## Context

The maintainer's decision, on 2026-11-23, was to license product releases after the rename under FSL-1.1-ALv2 and to develop the hosted-service platform in private behind a public trust set. Today's code has four facts that block an FSL release:

1. **One AGPL-only contribution.** It is 37 lines in `src/exomem/edit.py` and 82 lines in `tests/test_edit_outside_governed_root.py`. The author never signed the agreement.
2. **PyMuPDF.** It is AGPL-3.0, or a paid licence. The product imports it at five sites in `extract.py`, `document_identity.py` and `media_types.py`. They read page text, render a page for OCR, read the document information dictionary and the XMP packet, detect encryption, and recognise MuPDF allocator failures. It ships in the `cloud` image.
3. **pillow-heif binary wheels.** They are GPL-2.0, because they bundle the x265 encoder. They ship in `image-codecs`, so in the `cloud`, `ml` and `cuda` images. The product only decodes HEIF.
4. **PyAV binary wheels.** The package says BSD-3-Clause, but the wheel bundles libx264 and libx265 (GPL). PyAV ships in `frame-codecs` and `media-cpu`, so in the `cloud` image. The product decodes audio and video in seven modules and never encodes video.

FSL's own text does not mention combination with copyleft code. AGPL §5(c) and §10 forbid adding restrictions to a conveyed work that includes AGPL code. So a mixed program cannot be conveyed under FSL while any third-party copyleft remains.

The agentic-engineering primary-source roster has no row for licensing or dependency licence work, so none is cited.

## Goals / Non-Goals

**Goals**
- Make the product and its published images FSL-ready, with no change in what users get.
- Change the contributor promise before anyone signs.
- Define the trust set, so the platform can go private without turning the security claims into "trust us".

**Non-goals**
- Relicensing anything published before the cut. That is impossible, and it is not attempted.
- Choosing the new product name, or filing a trademark. The naming work and the maintainer own those.
- Changing what pip users get from PyPI dependency wheels. A self-hoster who installs with pip makes their own combination, and we do not convey it. The constraint binds what we publish.

## Decisions

### D1. FSL-1.1-ALv2, cut at the rename

FSL is a standard text with a fixed definition of competing use and a two-year conversion. BUSL needs a custom "additional use grant" for each licensor, and conversion can wait up to four years. A proprietary module licence on the Bitwarden model fences features, not the product, and breaks free self-hosting of those features.

The ALv2 variant carries a patent grant, which the MIT variant lacks.

The cut goes with the rename because the repository identity changes then anyway. If the first commercial release comes before the rename, cut at that release instead.

### D2. Contributor promise: OSI within two years

§3 becomes "available under an OSI-approved licence no later than the second anniversary of its release". That matches FSL's own conversion date exactly, so every FSL release keeps the promise. Dropping the promise entirely would cost contributor trust and gain nothing.

### D3. PDF: pypdfium2 for text and rendering, pypdf for the XMP packet

- **pypdfium2** binds PDFium, the PDF engine Chrome uses; it is BSD-3-Clause and Apache-2.0, and so are its bundled binaries. It covers page text, page rendering at a given scale, the information dictionary and password detection.
- **PDFium has no public call for the XMP packet.** pypdf (BSD-3-Clause, pure Python) reads it, and the packet still goes to the existing `defusedxml` parser.
- **Rejected options:** pdfminer.six (slower, weaker on complex layouts), markitdown's PDF path (already judged weaker), and buying an Artifex licence (a recurring cost, and it binds the Cloud only).
- **Allocator failures.** MuPDF's allocator message has no PDFium equivalent. The new code maps PDFium's memory errors and Python's `MemoryError` to the same `MediaMemoryExhausted` outcome, and the process memory cap stays the backstop.
- **Proof.** Run the existing PDF tests. Also compare extraction on public PDFs of each kind: a text layer, a scanned page, a mixed document, an encrypted file and a damaged file. The comparison counts characters, the page markers and the OCR page count, side by side with the old output.

### D4. HEIF: pi-heif

pi-heif is the decode-only variant of pillow-heif, from the same maintainer. Its source is BSD-3-Clause, and its wheels bundle LGPL libheif and libde265 without x265. `register_heif_opener` keeps its name, so `image_codecs.py` changes only its import.

### D5. Audio and video: PyAV built against a decode-only LGPL FFmpeg in the image build

| Option | Cost | Verdict |
|---|---|---|
| Build FFmpeg from source with no GPL components (no `--enable-gpl`, no x264 or x265), then build PyAV against it in the Docker builder stage | Product code unchanged. FFmpeg's own H.264 and HEVC decoders are LGPL. Adds build time, and we track FFmpeg security releases ourselves. | **Chosen** |
| Replace PyAV with `ffmpeg` and `ffprobe` subprocesses | A separate program avoids the combination question, but means rewriting seven modules, changing performance and error mapping | Rejected: large rewrite, user-visible risk |
| Keep the PyPI wheel and delete the bundled encoders | Breaks dynamic linking, and is fragile | Rejected |

The choice of build is pinned in the Dockerfile and checked by the image scan (D7). If PyAV's next release offers an official LGPL-only wheel, the build stage goes away.

### D6. The AGPL-only contribution: a two-lane rewrite

1. One lane reads the contributed lines and their test. It writes a behaviour description with no code: inputs, outputs, error codes and message intent.
2. A second lane, which never sees the original lines, deletes them and implements and tests the behaviour from that description.

Short identifiers and error codes are functional, so they may stay identical.

### D7. No new licence gate; a scan at the cut

**The scan.** Before the licence cut, list every shared library and Python distribution in each published image (lean, ml, cuda, cloud, the custodian and the controller) with its licence. Record the result as release evidence.

**Why no gate.**
- The two bundling risks are gone by construction: the codec layer is built from source in our Dockerfile, and pi-heif has no encoder.
- A custom CI gate would need its own list of library names to match against. That is a maintained list deciding what a file means, which the repository's rules forbid without a real need.
- The residual risk is a later change back to binary wheels. The `product-licensing` requirement and review cover it.

### D8. Trust set and custodian split

- **The problem.** The security model document links the controller's core today: `reconcile.py`, `manifests.py`, `decide.py` and others, about 56% of the controller source. Keeping those public would keep the orchestration public.
- **The trust set** is the set of enforcement points that bind a private controller: the admission, deletion and operator-access policies, the deletion-grant contract and verifier, and the key custodian.
- **The custodian** runs today as the controller image with another command. It becomes its own package and image, built and attested by public CI. `custodian.py` depends only on a few helpers from `manifests.py` and on small modules (`deletion_grant`, `errors`, storage), so the split is bounded.
- **The security document** is rewritten so that each claim links trust-set code. A claim about controller behaviour is labelled as the operator's statement and names the policy that bounds it.
- **Signing.** Private repositories on the free plan cannot create GitHub artifact attestations. The private controller image is signed with our own key, and the admission policy trusts that key. That policy is off today; `make-cloud-cells-operator-resistant` turns it on.
- **Dependency direction.** Private code may read public code and releases, never the reverse.

## Risks / Trade-offs

- **The PDF swap could change extraction output** on unusual files. The comparison in D3 runs before merge, and the old PyMuPDF path is not kept as a fallback, because a fallback would keep the AGPL dependency.
- **The custom FFmpeg build becomes our security surface.** It is pinned and rebuilt on each FFmpeg security release, and the same Dockerfile owns it.
- **Published code stays AGPL forever.** Anyone can fork the last AGPL release and the 2026-11 platform snapshot. The change limits only future work. That is the accepted trade.
- **Losing the "open source" label** excludes OSI-only programmes and some contributors. The maintainer accepted this cost.
- **Legal questions stay open.** No counsel is engaged, by the maintainer's choice. The open questions are listed in the maintainer's private decision record, for later review.

## Migration Plan

1. Change the contributor promise now (this PR).
2. Remove the third-party copyleft, in one batch: the contribution rewrite, the PDF swap, the HEIF and codec layer, and the image scan.
3. Make the licence cut at the rename: `LICENSE`, metadata, labels, README, source offer and terms, plus one public statement.
4. Move the platform after the two cloud security changes ship. Split out the custodian, rewrite the security document, create the private repositories, move the code, rotate the keys once, and keep the old tags reachable.

**Rollback.** Steps 1 and 2 change no licence of existing code; reverting them is an ordinary revert. Step 3 is reversible for future releases only: a later release may return to an OSI licence. Step 4 keeps the public tree until two releases have deployed from the private fleet.
