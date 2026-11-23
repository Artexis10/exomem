# Tasks

## 1. Contributor promise

- [x] 1.1 Change CLA §3 to promise OSI availability no later than the second anniversary of each release that includes a contribution.
- [x] 1.2 Confirm that the signature store holds no signatures when the change merges.

## 2. Remove third-party strong copyleft

- [ ] 2.1 Write a behaviour description of the contributed lines in `edit.py` and their test, with no code.
- [ ] 2.2 From that description only, replace the contributed lines and their test with new code and new tests; confirm by blame that no contributed line remains.
- [ ] 2.3 Replace PyMuPDF with pypdfium2 (text, rendering, information dictionary, encryption) and pypdf (XMP packet); keep the memory-exhaustion outcome.
- [ ] 2.4 Compare PDF extraction before and after on public text, scanned, mixed, encrypted and damaged PDFs; record characters, page markers and OCR page counts.
- [ ] 2.5 Replace pillow-heif with pi-heif.
- [ ] 2.6 Build a decode-only LGPL FFmpeg and PyAV against it in the image builder; keep the PyAV version bound; prove audio, video-frame, preview and metadata paths in the built image.
- [ ] 2.7 Scan every published image (lean, ml, cuda, cloud, cellctl) for GPL or AGPL shared libraries and distributions; record the result.

## 3. Licence cut (at the rename, or at the first commercial release if earlier)

- [ ] 3.1 Replace `LICENSE` with FSL-1.1-ALv2; keep the AGPL-3.0 text as the prior licence and name the last AGPL commit.
- [ ] 3.2 Update `pyproject.toml` licence metadata, OCI image labels, the README licence map and badge, `TRADEMARK.md` and the CLA repository reference.
- [ ] 3.3 Update the hosted service's source offer and terms wording in the control-plane repository.
- [ ] 3.4 Publish one plain statement: what changed, from which release, that self-hosting stays free, and the two-year Apache-2.0 conversion.

## 4. Platform boundary (after `contain-cloud-controller-compromise` and `make-cloud-cells-operator-resistant` ship)

- [ ] 4.1 Split the key custodian into its own package and image, built and attested by public CI.
- [ ] 4.2 Rewrite `docs/cloud-security-model.md` so every code link resolves to the trust set or the product, and label operator statements.
- [ ] 4.3 Create the private fleet, operations and control-plane repositories with a self-hosted runner and an image-signing key that the admission policy trusts.
- [ ] 4.4 Move the fleet code, workflows, specs and runbooks; decouple the 37 product test files and 10 workflows that reference `infra/`; keep the conformance suite public.
- [ ] 4.5 Rotate the long-lived keys once, with the operations data move.
- [ ] 4.6 Keep the published fleet tags and images reachable; remove the moved paths from `main` only after two releases deploy from the private fleet.
