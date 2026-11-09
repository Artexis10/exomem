## MODIFIED Requirements

### Requirement: Bounded Payload

The tool SHALL bound its payload: `max_frames` defaults to 8 and is clamped to
`[1, cap]` where the cap is the deployment's `video_frames_tool_cap` setting, which
`EXOMEM_VIDEO_FRAMES_TOOL_CAP` overrides and which defaults to 16 (unparseable or
non-positive values fall back to the default cap). Clamping SHALL be silent, with the
effective value reported in the metadata. Each returned JPEG's longest side SHALL be at
most `max_side` pixels, or the video's own size when `max_side` is `"original"`, as
`read_media` takes it for images; when the caller gives no `max_side`, the deployment's
`video_frames_max_side` setting SHALL decide it, defaulting to 768. An invalid
`max_side` SHALL be refused with an error that names the accepted form. The metadata
SHALL report the effective long side as `max_side_effective`. When the call's frames
reach the deployment's per-image inline limit (`image_view_inline_max_bytes`) or
per-call limit (`image_view_max_call_bytes`), both measured as base64, the server SHALL
return the earlier frames, list the timestamps of the frames left out as `not_shown`,
and never drop them silently; when not even the first frame fits, it SHALL refuse the
call with `FRAME_TOO_LARGE`, naming a smaller `max_side` as the remedy. The process that
answers the call SHALL apply these limits after a local or Cloud read alike. An Exomem Cloud cell, whose frames worker sizes its
memory admission from a fixed edge, MAY keep that edge whatever the caller asks, and
SHALL report it as `max_side_effective`.

#### Scenario: Excessive request clamps to the cap

- **WHEN** a client calls the tool with `max_frames=999`
- **THEN** at most 16 frames return and the metadata reports the effective maximum

#### Scenario: Oversized source frames are downscaled

- **WHEN** a client names no `max_side` and the video's frames exceed 768px on their longest edge
- **THEN** every returned JPEG's longest edge is 768px and `max_side_effective` is 768

#### Scenario: The caller asks for the video's own size

- **WHEN** a client calls `read_media` with `kind: "video"` and `max_side: "original"` on a local server
- **THEN** each returned JPEG keeps the video's frame size and `max_side_effective` is `"original"`

#### Scenario: Frames past the call's byte limit

- **WHEN** four frames at the requested size exceed `image_view_max_call_bytes`
- **THEN** the frames that fit return in time order and `not_shown` lists the timestamps of the rest

#### Scenario: Exomem Cloud keeps its fixed edge

- **WHEN** a client asks an Exomem Cloud cell for frames with `max_side: "original"`
- **THEN** the frames are at most 768px on their longest edge and `max_side_effective` is 768
