# Proposal

## Why

Today the product and the code that runs the hosted service are public under AGPL-3.0-or-later. AGPL lets any provider host the public product as a competing paid service, provided it publishes its changes. Several open-source companies have lost their hosted market this way. The maintainer decided to keep the code readable and free to self-host, while making competing hosted use of new releases a licence breach. The maintainer also decided to stop publishing the hosted-service platform's future work.

The change lands now, for two reasons. The product name must change before the commercial launch, which forces a new repository identity anyway. And no contributor has signed the Contributor License Agreement yet, so its open-source promise can change at no cost to anyone.

## What Changes

- **BREAKING (licence):** releases of the product cut after the rename are licensed under FSL-1.1-ALv2 (Functional Source License 1.1, Apache-2.0 future licence). Each release becomes available under Apache-2.0 on the second anniversary of its publication. Every release published before the cut stays AGPL-3.0-or-later. The client plugins and the starter skill stay Apache-2.0.
- The Contributor License Agreement's open-source promise changes from "always OSI-licensed" to "OSI-licensed no later than the second anniversary of each release". This lands before the first signature.
- Before the licence cut, the product and the images published from this repository carry no third-party strong-copyleft code. Strong copyleft is a licence that requires the combined work to carry the same licence, such as GPL or AGPL. The work rewrites the one contribution licensed only under AGPL. It replaces the AGPL PDF library. It rebuilds the image audio and video codec layer and the HEIF decoder without the GPL encoders they bundle.
- The hosted-service platform develops in private from the cut. The platform is the fleet tooling here and the service control plane in its own repository. A public trust set stays in this repository: the admission policies, the deletion-grant contract and verifier, the key custodian and the cell-runtime conformance suite. The trust set lets customers check the Cloud security claims. Code published before the cut stays reachable.
- Public wording stops calling releases after the cut "open source". Instead it says "source-available" or "fair source", that self-hosting stays free, and when each release converts to Apache-2.0.

## Capabilities

### New Capabilities

- `product-licensing`: which licence each shipped artifact carries, the contributor promise, the third-party licence constraint on shipped artifacts, and the public licence wording.
- `cloud-platform-boundary`: what of the hosted-service platform stays public as the trust set, what develops in private, and the dependency direction between them.

### Modified Capabilities

None.

## Impact

- **Licence files:** `LICENSE`, `CLA.md`, `TRADEMARK.md`, the README licence map, `pyproject.toml` licence metadata, the OCI image labels, and the hosted service's source offer and terms (in the control-plane repository).
- **Dependencies:** PyMuPDF goes away, and permissive PDF libraries replace it. The HEIF decoder becomes decode-only. The audio and video codec layer in the published images is rebuilt against a decode-only LGPL FFmpeg. Text extraction output, OCR fallback and media metadata behaviour stay the same.
- **Code:** `src/exomem/edit.py`, where the contributed lines are rewritten; the PDF extraction, PDF identity and allocation-failure paths; `image_codecs.py`; the Dockerfile.
- **Repositories:** new private repositories for the fleet, its operations data and the service control plane, after the two in-flight cloud security changes ship. The fleet's workflows, tests and specs move with it, and 37 product test files and 10 workflows that reference `infra/` are decoupled.
- **Dependencies on other work:** the licence cut waits for the product rename. The platform move waits for `contain-cloud-controller-compromise` and `make-cloud-cells-operator-resistant` to ship.
