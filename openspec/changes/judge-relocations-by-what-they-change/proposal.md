## Why

A relocation moves pages and changes how links reach them. It never changes what a page says. On a copy of a real vault, a rename of about 1,500 pages to their titles refused more than a third of them with `SEMANTIC_CONTRACT_BLOCKED`. Almost every refusal came from debt that the pages already had: older pages with no semantic units and a path for an identity, and pages linking them with debt of their own. The move judged each such page in full. It used the subset rule only for a grandfathered page that has an exomem_id.

Two more defects sit on the move path:

- An older page without an exomem_id is in the activation baseline by its path and its bytes. A governed move changes both, so after a move the page lost its baseline. Its next edit then had to clear every debt it carried.
- Each move step derived every relation in the vault again to build its after corpus. On a generated vault of 10,000 notes, one move preflight took about 7 s.

## What Changes

- A relocation judges each page it reaches, the moved page and every dependent, by the set-based non-worsening rule. It refuses only an error key that the relocation adds, or a satisfied relation disposition that it breaks. This holds for a page outside the activation baseline too. The undo of a relocation uses the same rule.
- An older page without an exomem_id keeps its baseline through the governed moves that its lineage records. When the page's own path and bytes do not match, its lineage entries are removed one at a time. If the bytes then match the entry for an earlier path, the page is still grandfathered. Any other change to the page's bytes still ends the baseline. The edit path, its re-evaluation at commit and the posthoc audit read it this way.
- A move step patches the before corpus by the pages that the move changes (`SemanticCorpusContext.with_pages`). It no longer derives the whole vault again. The patched corpus equals a full derivation.

## Capabilities

### Modified Capabilities

- `semantic-write-contract`: how a relocation is judged, and how an older page keeps its activation baseline through a move.

## Impact

- Code: `src/exomem/semantic_writes.py` and `src/exomem/semantic_contract.py`.
- Behaviour: a relocation that was refused only for pre-existing debt now commits. Such a refusal reported that debt as blocking, and the commit now reports it as a warning.
- Data: none. The activation manifest and page lineage keep their format.
