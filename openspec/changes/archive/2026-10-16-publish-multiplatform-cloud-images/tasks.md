## 1. Delivery

- [x] 1.1 Merge with the `Cloud images` workflow green on `linux/amd64` and `linux/arm64`. Evidence: #1666 merged as c4cb58136 with "Cloud images on linux/amd64" and "Cloud images on linux/arm64" both successful.
- [x] 1.2 Check the first release after the merge: `docker buildx imagetools inspect` lists both platforms for `X.Y.Z-cloud` and `exomem-cellctl:X.Y.Z`, and `gh attestation verify` passes for each recorded index digest. Evidence: in v0.119.0, `0.119.0-cloud` (index sha256:591dd22e…) and `exomem-cellctl:0.119.0` (index sha256:925f833b…) each list linux/amd64 and linux/arm64, and `gh attestation verify` passes SLSA provenance for both digests, signed by `release-please.yml` at `refs/tags/v0.119.0`.
