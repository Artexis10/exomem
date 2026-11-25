# Tasks

## 1. Contributor promise

- [x] 1.1 Change CLA §3: each public version that includes a contribution is available under an OSI-approved licence no later than the second anniversary of the date it is first made available, and stays available; update the CONTRIBUTING summary to match.
- [x] 1.2 Confirm that the signature store holds no signatures when the change merges.

## 2. Remove third-party strong copyleft

- [x] 2.1 Write a behaviour description of the contributed lines in `edit.py` and their test, with no code.
- [x] 2.2 From that description only, replace the contributed lines and their test with new code and new tests; confirm by blame that no contributed line remains.
- [x] 2.3 Replace PyMuPDF with pypdfium2 (text, rendering, information dictionary, encryption) and pypdf (XMP packet) in extraction, document identity, the doctor's profile detection and dependency check, and deployment provenance; keep the memory-exhaustion outcome where the parent can tell it.
- [x] 2.4 Port the tests and fixture builder that need PyMuPDF; confirm no new skipped tests.
- [x] 2.5 Compare PDF extraction before and after on public text, scanned, mixed, user-password, owner-only-password, damaged and XMP-bearing PDFs; record characters, page markers, OCR page counts, refusal reasons and time per file.
- [x] 2.6 Replace pillow-heif with pi-heif.
- [x] 2.7 Build a decode-only LGPL FFmpeg with dav1d, and PyAV against it, in the image builder; keep the PyAV version bound; prove decoder parity with the old wheel and the audio, video-frame, preview and metadata paths in the built image; record the FFmpeg source and configure flags in the notices.
- [x] 2.8 Add a per-release check of the published images' Python distribution licences with an off-the-shelf tool against SPDX identifiers.
- [x] 2.9 List the shared libraries in every published image (lean, ml, cuda, cloud, cellctl) with their licences, with the operating-system layer in its own section; record the result.

## 3. Licence cut (at the rename, or at the first commercial release if earlier)

- [x] 3.1 Replace `LICENSE` with FSL-1.1-ALv2; keep the AGPL-3.0 text as the prior licence and name the last AGPL commit.
- [x] 3.2 Update `pyproject.toml` licence metadata and the OCI image labels to `FSL-1.1-ALv2 AND Apache-2.0`; update the README licence map and badge, `TRADEMARK.md`, the CLA repository reference, `docs/ARCHITECTURE.md`, and the image labels that the active `add-docker-distribution` change specifies.
- [x] 3.3 Update the hosted service's source offer and terms wording in the control-plane repository.
- [x] 3.4 Regenerate `docs/licensing/image-shared-libraries.md` from the first FSL release's published images on every platform (lean, ml, cuda, cloud, cellctl), with Unmapped and Strong copyleft at 0 in every row; record it before the release statement.
- [ ] 3.5 Publish one plain statement: what changed, from which release, that self-hosting stays free, and the two-year Apache-2.0 conversion.
- [ ] 3.6 Give the `cellctl` image the licence label and `/LICENSE` that every published artifact carries, or take it private with the platform move (section 4), before this change is archived.

## 4. Platform boundary (after `contain-cloud-controller-compromise` and `make-cloud-cells-operator-resistant` ship)

- [ ] 4.1 Split the key custodian into its own package and image, built and attested by public CI.
- [ ] 4.2 Rewrite `docs/cloud-security-model.md` so every code link resolves to the trust set or the product, and label operator statements.
- [ ] 4.3 Add a MODIFIED delta to `cloud-operator-access` so admission accepts the private controller image when the platform's release key signed it; keep public attestations for the trust-set images.
- [ ] 4.4 Create the private fleet, operations and control-plane repositories with a self-hosted runner and a release key held in a hardware-backed or cloud key store.
- [ ] 4.5 Move the fleet code, workflows, specs and runbooks; decouple the 37 product test files and 10 workflows that reference `infra/`; keep the conformance suite public.
- [ ] 4.6 Rotate the long-lived keys once, with the operations data move.
- [ ] 4.7 Keep the published fleet tags and images reachable; remove the moved paths from `main` only after two releases deploy from the private fleet.
