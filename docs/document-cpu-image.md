# CPU document/OCR image

Build the worker-base image with:

```sh
docker build --target documents-cpu -t exomem-documents-cpu .
```

It contains local PDF, DOCX, XLSX, PPTX and image OCR extraction only. The
image has PyMuPDF, MarkItDown's local Office converters, Tesseract and English
language data. It does not include torch, CUDA, faster-whisper or CTranslate2.
Its fixed runtime identity is UID/GID 10001, it has no declared volume or
listener, and its default command is `exomem --help` so it is compatible with a
read-only root filesystem.

This is a worker-base artifact. It is not published, selected automatically or
an authorization boundary: a hosted job still needs admission, a per-job
sandbox and worker authority from the caller. The image does not execute Office
macros, retrieve external document links or enable captioning, CLIP or ASR.

Run the offline acceptance check after a Docker build is available:

```sh
scripts/verify-document-cpu-image.sh
```

The command builds the synthetic inputs from readable Python/XML source in a
separate no-network generator container, running as the host user into its own
writable temporary directory. The fixed UID/GID 10001 extraction container then
mounts those generated inputs read-only. Both containers drop capabilities,
apply a 512 MiB/one-CPU bound and use a bounded temporary filesystem. The check
verifies that original hashes remain unchanged, rejects heavyweight/NVIDIA
distributions and records any attempted Python network egress before asserting
that none occurred. Each invocation creates its own temporary host directory,
Docker tag and default Buildx config, so concurrent acceptance runs do not share
cleanup targets. It prints the exact image ID, image size, `--help` startup time
and cgroup memory peak as local measurements only; they are not capacity or
latency guarantees.

The DOCX fixture includes a referenced external relationship to a synthetic
`.invalid` URL. The acceptance check confirms its visible link text survives
conversion while the egress trap records and rejects any attempted lookup or
connection.
