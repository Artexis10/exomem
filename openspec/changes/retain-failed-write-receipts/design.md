## Decisions

Use the original identity and retry window that the intake already records. Keep recent terminal failures in the same process index as applied saves. Load both on start. A matching resend returns the same reference and carries only the commit evidence the receipt records.

If an older release recorded duplicate origins, return the open receipt first. Otherwise return the latest surviving terminal receipt in the retry window. Dismissal removes only the chosen receipt. A later resend considers the remaining receipts in the same order, including after restart.

If the process cannot read the store at startup and has no loaded index, preserve the existing intake-unavailable fallback. The process cannot offer the same-reference guarantee until it can recognise the receipt.

Leave full refusal details on the existing governed receipt lookup. If a previously known row cannot be read, return its reference with an unknown commit effect. The failure notice directs the caller to that lookup. Explicitly dismissing a stored failure still follows the existing owner operation.

Derived indexes bound startup to the retry window and find surviving receipts by origin after dismissal. On an older store, idle startup uses the existing state index and reads open and applied receipts only. The first eligible save or owner dismissal builds the indexes. Before choosing a new execution, the first eligible save loads the indexed failures. If the service cannot finish this check on a known legacy store, it returns a retryable refusal. A prior release can ignore the indexes.
