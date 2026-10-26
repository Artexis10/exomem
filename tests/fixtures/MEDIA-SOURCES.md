# Public speech fixture sources

The speech fixtures verify the offline audio and video extraction boundary.
They are exposed integration fixtures, not held-out quality or model-selection data.

## Source and licence

Google FLEURS supplies the English validation recording under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
The [pinned dataset](https://huggingface.co/datasets/google/fleurs/tree/70bb2e84b976b7e960aa89f1c648e09c59f894dd) provides the recording and its transcript.
The [dataset card](https://huggingface.co/datasets/google/fleurs/blob/70bb2e84b976b7e960aa89f1c648e09c59f894dd/README.md) records the licence.

- Dataset: `google/fleurs`; configuration: `en_us`; split: `validation`.
- Dataset revision: `70bb2e84b976b7e960aa89f1c648e09c59f894dd`.
- Source utterance ID: `1530`; zero-based Parquet row: `31`.
- Source audio member: `11030046932175288954.wav`.
- Source audio bytes SHA-256: `3f5c0411675dc52be329d9fb9da5d971220a15d60b41e99f775a7170af3c062c`.
- Source Parquet: `parquet-data/en_us/validation-00000-of-00001.parquet`.
- Source Parquet SHA-256: `7c3eeb11a9597bd52cdc1b0d637e85389fe094cfd8763913e7bf4fdf7a853959`.

## Derived fixtures

`sample.wav` copies the verified PCM16 validation cache without another audio change.
The cache preserves the original rate and samples within one PCM16 quantisation unit.
It contains 78720 mono samples at 16000 Hz, for 4.92 seconds.

`sample.mkv` contains that same audio as lossless PCM16 and five generated grey frames.
PyAV 18.1.0 encoded the 64 by 64 pixel frames with the native CPU MPEG-4 encoder at one frame per second.
The decoded video audio matches all 78720 WAV samples exactly.
No model produced or selected the expected phrase in `media-samples/expected.json`.
That phrase is a contiguous substring of the publisher's transcript.

| Fixture | Bytes | SHA-256 |
|---|---:|---|
| `sample.wav` | 157484 | `c8b37e1bd791f59dd3f3584ae71e40badc3ab9cce9329523ecc0f95c3705c53f` |
| `sample.mkv` | 158879 | `e8734145296706cc1fb296d1a9d95e94c49d00e7d640346b5c7957b48f8fa1f9` |
