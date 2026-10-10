## 1. Delivery

- [ ] 1.1 Merge with the `Cloud images` workflow green on `linux/amd64` and `linux/arm64`.
- [ ] 1.2 Check the first release after the merge: `docker buildx imagetools inspect` lists both platforms for `X.Y.Z-cloud` and `exomem-cellctl:X.Y.Z`, and `gh attestation verify` passes for each recorded index digest.
