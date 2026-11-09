## Why

An agent can find a preserved photo's Source page but cannot look at the photo. `read_media` shows video frames only, and the other routes to an original fail in practice:

- The generic memory reader answers a JPEG with `UNREADABLE`, as a text reader should.
- `transfer_artifact` offers an `exomem://original/...` MCP resource, which most clients do not read, and it marked that resource as the preferred delivery.
- Its HTTPS link carries the whole signed grant, about 2,000 characters. An agent that copies the link corrupts it, and the download then answers `ORIGINAL_UNAVAILABLE`.

The result is that preserved photos are discoverable as metadata but unusable for analysis, which breaks the promise that preserved information stays usable by agents.

## What Changes

- `read_media` gains `kind: "image"`. It returns one to six stored images as MCP image content, upright and downscaled (longest side 1568 px by default, 256 to 2048), with each image's file name, capture time from EXIF, and original and shown pixel sizes. It reads each original through the same custody and full-disclosure release checks as an original download. The stored original never changes.
- A format the server cannot decode gets a refusal that names the download route.
- Camera raw (DNG) originals decode through LibRaw (`rawpy`, in the `image-codecs` extra) for image view, OCR, the visual index and the caption: the full sensor image, upright, refused before demosaic when it exceeds the pixel ceiling.
- The process that serves `/download` keeps each HTTPS grant in memory under a random 128-bit id, so the link reads `.../download?grant=<22 characters>`. The grant still decides every download. An unknown or expired id answers `DOWNLOAD_LINK_EXPIRED` and tells the person to ask for a fresh link. A process that does not serve `/download` keeps minting the full grant.
- `transfer_artifact` prefers the HTTPS link when one exists, because any client can hand a person a link while few clients read MCP resources.
- Video frames follow the same sizing: the caller's `max_side` sets the frame long side up to `"original"`, the default and the frame cap become settings, the image view byte settings bound the returned frames, and the metadata reports the effective side. Exomem Cloud keeps its worker's fixed 768 px edge and reports it.

Out of scope: camera raw formats other than DNG, and the ChatGPT connector refresh that exposes the new kind there, which the owner performs after release.

## Capabilities

### New Capabilities

- `preserved-image-access`: agents see stored images as image content, and people get short download links for originals.

### Modified Capabilities

- `video-frames`: the frame size and frame cap come from the caller and settings instead of code.

## Impact

- Code: `src/exomem/image_view.py`, `src/exomem/download_links.py`, `src/exomem/commands.py`, `src/exomem/operation_entries.py`, `src/exomem/artifact_recovery.py`, `src/exomem/server_transfer.py`, `src/exomem/image_codecs.py`, `src/exomem/video_frames.py`, `src/exomem/video_frames_worker.py`, `src/exomem/media_types.py`, `src/exomem/extract.py`; `rawpy` joins the `image-codecs` extra (no Intel macOS wheel, so those Macs keep raw undecodable).
- Tool surface: `read_media` adds the `image` kind (+334 B of schema) and `max_side` on the video kind (+43 B); the ChatGPT connector needs a refresh to see them.
- Undo: revert the release. Old long links keep working because a full grant still resolves.
