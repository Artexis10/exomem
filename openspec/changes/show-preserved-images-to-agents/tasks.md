## 1. Image content

- [x] 1.1 Add `kind: "image"` to `read_media`, returning stored images as MCP image content through the original-download custody and release checks
- [x] 1.2 Refuse undecodable formats, oversized images and Cloud cells with an error that names the download route
- [x] 1.3 Test that an agent receives an upright, downscaled photo and that the stored original is unchanged
- [x] 1.4 Decode only the registry's image formats, outside the shared recovery slots, one image at a time
- [x] 1.5 Let the caller choose the size up to the original and a crop region; take defaults and the inline and per-call limits from settings, with no image count or size ceiling in code

## 2. Short download links

- [x] 2.1 Map each HTTPS grant to a random id in the process that serves `/download`
- [x] 2.2 Answer an unknown or expired id with `DOWNLOAD_LINK_EXPIRED`
- [x] 2.3 Prefer the HTTPS delivery in `transfer_artifact`
- [x] 2.4 Test that a short link serves the exact original bytes

## 3. Video frame size

- [x] 3.1 Let the caller's `max_side` on `read_media` set the video frame long side up to `"original"`, defaulting to the `video_frames_max_side` setting
- [x] 3.2 Make the frame cap the `video_frames_tool_cap` setting, keep `EXOMEM_VIDEO_FRAMES_TOOL_CAP` as its override, and bound returned frames by the image view byte settings
- [x] 3.3 Report `max_side_effective` and `not_shown`; keep Cloud at its worker's fixed edge and report it

## 4. Camera raw

- [x] 4.1 Decode `.dng` through LibRaw in `image_codecs`, full size and upright, refused before demosaic past the pixel ceiling, and list it in the image registry
- [x] 4.2 Give the caption model the decoded image instead of the path
- [x] 4.3 Add a synthetic portrait DNG to the media samples and the Cloud image gate

## 5. Acceptance

- [ ] 5.1 After release, show two stored photos through `read_media` in Claude and confirm the model describes them
- [ ] 5.2 After the ChatGPT connector refresh, show the same two photos in ChatGPT
