---
exomem_id: 0bf1ec83-b37b-5823-8561-5250e34d300e
name: budget-reconciliation
description: "Matches a month's bank statement against the ledger, finds missing, duplicate and misfiled transactions, and reports the difference. Use when the user closes a month, when a statement does not match the books, or when a charge looks wrong."
status: active
created: 2026-02-26
updated: 2026-07-08
---

# Budget reconciliation

How I want my monthly accounts checked.

## When it applies

It is the end of a month, a balance looks wrong, or I spot a charge I do not recognise.

## When it does not apply

- Choosing a savings strategy or a bank.
- Changes to the tool that holds the ledger.
- Forecasting future spending.

## Steps

1. Ask which account and which month. Take the statement import from my notes or from the file I give you.
2. Compare the statement total to the ledger total for the same account and period.
3. List transactions that appear only in the statement, only in the ledger, or twice.
4. Look for misfiled ones: right amount, wrong category or account.
5. For each difference, propose one correction. Do not apply them yet.
6. After I agree, apply the corrections and show the new totals side by side.
7. Save a short reconciliation note with the month, the difference found and what was done.

## Rules

- Work in the minor unit of the currency to avoid rounding.
- Never delete a transaction. Mark it duplicate.
- If the difference cannot be explained, say so and leave it open.

## Where results go

A dated reconciliation note under Notes/Productions. Corrected transactions go to the ledger.

## Format of the result

Give a three-line summary first: statement total, ledger total and difference. Then a table of the differences with the proposed fix for each. After the corrections, repeat the three lines so that I can see the difference is zero or explained.

## Common mistakes

- Matching by amount alone and pairing the wrong two transactions.
- Treating a refund as a duplicate of the original charge.
- Applying corrections before I agreed to them.
- Rounding a conversion of foreign currency instead of keeping the rate used.

## Example differences

- A coffee shop charge appears in the statement on the last day of the month and in the ledger on the first day of the next. The fix is to move the ledger date.
- A subscription renewal appears twice in the ledger because two exports overlapped. The fix is to mark one as duplicate.

