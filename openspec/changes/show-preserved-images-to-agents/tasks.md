## 1. Image content

- [x] 1.1 Add `kind: "image"` to `read_media`, returning stored images as MCP image content through the original-download custody and release checks
- [x] 1.2 Refuse undecodable formats with an error that names the download route
- [x] 1.3 Test that an agent receives an upright, downscaled photo and that the stored original is unchanged

## 2. Short download links

- [x] 2.1 Map each HTTPS grant to a random id in the process that serves `/download`
- [x] 2.2 Answer an unknown or expired id with `DOWNLOAD_LINK_EXPIRED`
- [x] 2.3 Prefer the HTTPS delivery in `transfer_artifact`
- [x] 2.4 Test that a short link serves the exact original bytes

## 3. Acceptance

- [ ] 3.1 After release, show the carnivore and X3 progress photos through `read_media` in Claude and confirm the model describes them
- [ ] 3.2 After the ChatGPT connector refresh, show the same photos in ChatGPT
