## 1. Titles
- [x] 1.1 Keep the display title beside the lower-cased title key in the link resolver, in every build, update and store-mode path.
- [x] 1.2 Add `render_title_links` with the per-link rule and the span map, and `stored_range` for the inversion.

## 2. Read
- [x] 2.1 Show body and unit links by title in `read_page` on the public tool shape, for a page released in full.

## 3. Write
- [x] 3.1 Count `old_text` in the shown body first, then in the stored body, for the public tool shape, in the surgical edit, the batch edit and the validate-only preview.
- [x] 3.2 Normalize a `[[Title]]` in an observation unit to the path form, and return its unresolved-link warnings.
- [x] 3.3 Put back the stored bytes of each link that `new_text` repeats unchanged from the matched text.

## 4. Proof
- [x] 4.1 Test each render branch, the inverse map, the refused cut, and the public tool shape end to end.

## 5. Delivery
- [ ] 5.1 Archive this change after the rebase onto the merged tool-surface change.
