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

§3 now covers each version that includes the contribution and that the maintainer makes available to the public. Each such version becomes available under an OSI-approved licence no later than the second anniversary of the date it was first made available, and stays available under that licence. FSL grants Apache-2.0 "on the second anniversary of the date we make the Software available", so both clocks start on the same date. A version that is never made available, such as a private fleet build, is outside both. Dropping the promise entirely would cost contributor trust and gain nothing.

### D3. PDF: pypdfium2 for text and rendering, pypdf for the XMP packet

- **pypdfium2** binds PDFium, the PDF engine Chrome uses; it is BSD-3-Clause and Apache-2.0, and so are its bundled binaries. It covers page text, page rendering at a given scale, the information dictionary and password detection.
- **PDFium has no public call for the XMP packet.** pypdf (BSD-3-Clause, pure Python) reads it, and the packet still goes to the existing `defusedxml` parser.
- **Rejected options:** pdfminer.six (slower, weaker on complex layouts), markitdown's PDF path (already judged weaker), and buying an Artifex licence (a recurring cost, and it binds the Cloud only).
- **Allocator failures.** MuPDF raises on a failed allocation; PDFium usually aborts the process. The PDF work already runs in child processes under a memory cap. The memory-exhausted outcome therefore comes from how the parent classifies a child that dies under the cap, not from matching library messages. Where the parent cannot tell a memory death from a crash, the implementation says so. Either way a page whose rendering or OCR fails for a resource reason never disappears from a completed extraction: today `_ocr_pdf_page` re-raises `MemoryError` and the MuPDF allocator failure but turns every other error into an empty page, and the new code must not do that for a resource failure. A page the renderer refuses because the page itself is damaged keeps today's behaviour: the rest of the document completes.
- **Other users of the old library:** the doctor's profile detection and dependency check, and deployment provenance. Without updating them, the automatic profile would silently drop from media to hybrid.
- **Tests.** Several test files skip themselves when PyMuPDF is missing, and one fixture builder uses it. They move to the new libraries, so the suite does not turn green by skipping. A rise in skipped tests counts as a failure.
- **Proof.** Run the existing PDF tests with no new skips. Also compare extraction, old against new, on public PDFs: a text layer, a scanned page, a mixed document, a user password, an owner-only password, a damaged file and an XMP packet. Record characters, page markers, OCR page count, the refusal reason and the time per file, because pypdf adds a second parse inside a child with a deadline.

### D4. HEIF: pi-heif

pi-heif is the decode-only variant of pillow-heif, from the same maintainer. Its source is BSD-3-Clause, and its wheels bundle LGPL libheif and libde265 without x265. `register_heif_opener` keeps its name, so `image_codecs.py` changes only its import.

### D5. Audio and video: PyAV built against a decode-only LGPL FFmpeg in the image build

| Option | Cost | Verdict |
|---|---|---|
| Build FFmpeg from source with no GPL components (no `--enable-gpl`, no x264 or x265), then build PyAV against it in the Docker builder stage | Product code unchanged. FFmpeg's own H.264 and HEVC decoders are LGPL. Adds build time, and we track FFmpeg security releases ourselves. | **Chosen** |
| Replace PyAV with `ffmpeg` and `ffprobe` subprocesses | A separate program avoids the combination question, but means rewriting seven modules, changing performance and error mapping | Rejected: large rewrite, user-visible risk |
| Keep the PyPI wheel and delete the bundled encoders | Breaks dynamic linking, and is fragile | Rejected |

The PyPI wheel also bundles dav1d (AV1), libvpx, libopus and opencore-amr. FFmpeg has no software AV1 decoder of its own, so the build links dav1d (BSD-2-Clause). FFmpeg's native decoders cover VP8, VP9, Opus and AMR. Parity is proved by comparing the decoders PyAV reports in the old wheel and in the new build. The build enables assembly, so decode speed does not drop. The image notices record the FFmpeg source tarball, its checksum and the configure flags, which is the LGPL source obligation.

The build is pinned in the Dockerfile and checked by the scan in D7. If PyAV later offers an official wheel without GPL encoders, the build stage goes away.

### D6. The AGPL-only contribution: a two-lane rewrite

1. One lane reads the contributed lines and their test. It writes a behaviour description with no code: inputs, outputs, error codes and message intent.
2. A second lane, which never sees the original lines, deletes them and implements and tests the behaviour from that description.

The rewrite covers lines that blame attributes to the contributor, and also maintainer-edited lines that still carry the contribution's expression: its messages, its normalisation and its comment. Short identifiers and error codes are functional, so they may stay identical. A rewrite avoids depending on a third party's signature. Asking the contributor to sign the agreement can still happen in parallel.

### D7. A per-release licence check, plus a bundled-library scan

**Per release.** Each published image's Python distributions are checked against their declared SPDX licence identifiers. trivy, already pinned in CI, reads installed Python package metadata as well as operating-system packages, so it is the first candidate for this check; one run confirms it. SPDX identifiers are a closed set fixed by a specification, so checking them is not the kind of word list the repository's C6 rule forbids. The release fails when a distribution in the product's imported code is licensed only under GPL or AGPL. A distribution offered under a choice that includes a non-copyleft licence passes and records the licence elected; `text-unidecode` (Artistic License or GPL) is the current case. A library under a linking exception passes wherever it ships; libgomp inside the rawpy wheel is the current case.

**Bundled libraries.** No off-the-shelf tool sees libraries bundled inside wheels. So the shared libraries inside each published image are listed with their licences at the cut, and again whenever a wheel that bundles native libraries changes or the base image changes. The images are lean, ml, cuda, cloud and cellctl, plus the custodian once task 4.1 creates it. The two known bundling risks are gone by construction: the codec layer is built from source in the Dockerfile, and pi-heif has no encoder.

**Operating-system layer.** The base image's programs (shell, core utilities, package manager) are separate programs, aggregated in the image rather than combined with the product. The GCC runtime libraries carry a linking exception. Both are recorded in the scan record, as the requirement says.

### D8. Trust set and custodian split

- **The problem.** The security model document links the controller's core today: `reconcile.py`, `manifests.py`, `decide.py` and others, about 56% of the controller source. Keeping those public would keep the orchestration public.
- **The trust set** is the set of enforcement points that bind a private controller: the admission, deletion and operator-access policies, the deletion-grant contract and verifier, and the key custodian.
- **The custodian** runs today as the controller image with another command; `contain-cloud-controller-compromise` decision D9 chose that. This change reverses it: the custodian becomes its own package and image, built and attested by public CI. `custodian.py` depends only on a few helpers from `manifests.py` and on small modules (`deletion_grant`, `errors`, storage), so the split is bounded.
- **The security document** is rewritten so that each claim links trust-set code. A claim about controller behaviour is labelled as the operator's statement and names the policy that bounds it.
- **Signing.** GitHub creates artifact attestations for private repositories only on Enterprise Cloud. So the private controller image is signed with our own key, and the admission policy trusts that key. This conflicts with a requirement that `make-cloud-cells-operator-resistant` adds to `cloud-operator-access`: admission accepts an image only when it carries a GitHub artifact attestation from this repository's release workflow on a release ref. Once that change ships, this change adds a MODIFIED delta to `cloud-operator-access`. The delta accepts either that attestation or a signature by the platform's release key, for the controller image only. The trade-off: whoever holds the release key can sign any controller image. The key therefore lives in a hardware-backed or cloud key store, signs only in the private release workflow, and the trust-set images keep public attestations.
- **Dependency direction.** Private code may read public code and releases, never the reverse.

## Risks / Trade-offs

- **The PDF swap could change extraction output** on unusual files. The comparison in D3 runs before merge, and the old PyMuPDF path is not kept as a fallback, because a fallback would keep the AGPL dependency.
- **The custom FFmpeg build becomes our security surface.** It is pinned and rebuilt on each FFmpeg security release, and the same Dockerfile owns it.
- **The platform release key can sign any controller image.** D8 keeps it in a hardware-backed or cloud key store and lets it sign only in the private release workflow; trust-set images keep public attestations.
- **Published code stays AGPL forever.** Anyone can fork the last AGPL release and the 2026-11 platform snapshot. The change limits only future work. That is the accepted trade.
- **Losing the "open source" label** excludes OSI-only programmes and some contributors. The maintainer accepted this cost.

## Migration Plan

1. Change the contributor promise now (this PR).
2. Remove the third-party copyleft, in one batch: the contribution rewrite, the PDF swap, the HEIF and codec layer, and the image scan.
3. Make the licence cut at the rename: `LICENSE`, metadata, labels, README, source offer and terms, plus one public statement.
4. Move the platform after the two cloud security changes ship. Split out the custodian, rewrite the security document, create the private repositories, move the code, rotate the keys once, and keep the old tags reachable.

**Rollback.** Steps 1 and 2 change no licence of existing code; reverting them is an ordinary revert. Step 3 is reversible for future releases only: a later release may return to an OSI licence. Step 4 keeps the public tree until two releases have deployed from the private fleet.
