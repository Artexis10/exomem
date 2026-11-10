-- Mirrors the deletion grant column that Substrate adds for automate-cell-storage-deletion (task 2.1).
-- It stands in for that migration until the contract pin in substrate-contract.json reaches a
-- Substrate commit that has it; the pinned file then replaces this one.

-- The deletion grant: an Ed25519 JWT that Substrate signs when it deletes a cell.
ALTER TABLE exomem_cloud_cells ADD COLUMN deletion_grant text;

-- No grants stanza: cellctl already reads the whole table, and it gets no write on this column.
