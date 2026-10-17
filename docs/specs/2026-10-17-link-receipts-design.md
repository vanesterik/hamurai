# Linking receipts: design

Issue #4. It builds on the booking MCP (`docs/specs/2026-10-17-hamurai-booking-mcp-design.md`), whose constraints, anti-goals and API section apply here too.

## Intent

The user uploads receipts with the Hamurai mobile app, before the quarter's booking session. Linking each one to its transaction by hand in the web app is tedious. The tool proposes which receipt belongs to which transaction for a period, and links what the user approves.

The user wants the ability, whatever it finds: a check on 2026-10-17 found only one recent unlinked receipt. *(The user's words: "I just want to have this ability".)*

## Goals

- For a period, propose a receipt for each transaction (or card line) that one matches, by amount and date, with the evidence.
- Link the approved receipts in Hamurai.
- Work on any transaction in the period, booked or not, so a quarter that is already booked (Q3 2026) can be done afterwards. *(The user's decision.)*

## Non-goals

- **Unlinking.** The unlink request was not recorded. A wrong link is undone in the web app. *(Builder's call; it can follow once the request is recorded.)*
- **Receipts Hamurai read no amount from** (most PDFs): they can't be matched, and are only counted.
- **Uploading.** The user uploads with the mobile app.
- **Reading receipt files.** The tool never downloads a file or reads its `secret`.
- **Changing bookings.** Linking a receipt never touches a booking.

## Anti-goals

- **A receipt linked without the user's approval.**
- **A receipt picked by a guess** when more than one could fit.
- **A file's `secret` reaching tool output, logs or the repository.**

## What Hamurai knows

Recorded from the web app and checked read-only on 2026-10-17.

- **A receipt is a file.** For an image, Hamurai reads the receipt and keeps `metadata.amountInCents` (positive, in cents) and `metadata.date` (`YYYY-MM-DD`). It keeps no supplier. Most PDFs have no amount (111 of the 200 newest files).
- `GET /api/v2/administrations/{admin}/files` lists the files newest first, 100 at a time. `offset=100` gives the next hundred; `page`, `size` and filters are ignored. Each file has `id`, `createdAt`, `name`, `mimeType`, `metadata`, `archivedAt`, and the IDs of what it is linked to: `transactionId`, `invoiceId`, `orderId`, `journalRecordId`, `estimateId`, `reminderId`, `billableItemSheetId`, `bankTransactionImportId`. **A file is unlinked when all of those are null**, and it isn't archived. Of the 200 newest, 24 were unlinked.
- `POST /api/v2/administrations/{admin}/files/{fileId}/attach` with `{"transactionId": …}` links a file to a **bank transaction** (not to a booking). It answers `200` with the file, now with `transactionId` set. A transaction can hold several files.
- A transaction's `hasFiles` and `filesStatus.nrOfFiles` say whether it has files. Both are in the `GET /transactions` list, not only in the single-transaction answer (checked on Q3 2026: 5 of 87 had files).
- `GET /reconciliations?transactionId=` returns each booking's `description`: for a split made by `book_split`, the merchant's.
- `GET /transactions/{id}/suggested-files` gives Hamurai's own suggestions, at most 25, ranked by the transaction's total. It's useless for a card collection, and not used.
- File answers include a `secret`, used for downloading. It is never kept or returned.

## Matching

**Targets.** Every expense in the period is a target, booked or not.

- **A bank transaction** with one booking or none is one target, with its amount and date. One that already has a file is skipped.
- **A split transaction** (more than one booking, such as a card collection) has one target per booking, with the booking's amount and description (for a card split, the merchant's). Its files don't say which booking they belong to, so a split's targets are never skipped. A receipt that is already linked can't be proposed again, so nothing is linked twice.
- Transactions with a positive amount (income) and positive bookings (card credits) are not targets: receipts are for expenses.

**A match** is an unlinked receipt whose amount equals the target's amount (made positive) and whose date falls in the target's window:

| Target | Window for the receipt's date |
|---|---|
| Bank transaction | From 30 days before its date to 3 days after. *(Builder's call: a shop payment is the same day, an invoice can be paid weeks later.)* |
| Line of a split | From 45 days before the transaction's date to that date. *(Builder's call: a booking from a card split carries the collection's date, and the collection comes up to about 45 days after the purchase.)* |

**Confidence.**

| Level | When |
|---|---|
| `confident` | The receipt matches exactly one target, and that target matches exactly one receipt. Lines of one split with the same amount count as one group: as many receipts as there are lines in it are all confident, since each would be linked to the same transaction. *(Builder's call.)* |
| `ambiguous` | A receipt matches more than one target, or a target matches more than one receipt. Each is listed with its candidates. The user picks one or none. |

Unlinked receipts with an amount that match no target are listed as `unmatched`, and receipts without an amount are counted, so the user can see what was left.

Receipts are read only as far back as they could matter: the listing stops at the first file created more than 45 days before the period's start. *(Builder's call: a receipt is uploaded after its date, so its `createdAt` is never earlier.)*

## Tools

| Tool | What it does | Returns |
|---|---|---|
| `propose_receipts(start_date, end_date)` | Reads the period's transactions, the bookings of its split transactions, and the unlinked receipts, and matches them. **Writes nothing.** | `confident` pairs, `ambiguous` groups, `unmatched` receipts, and the count of receipts without an amount. A receipt shows its file ID, name, date and amount; a target shows its transaction ID, date, amount, counterparty or merchant, and whether it is a line of a split. |
| `link_receipts(links)` | Takes a list of `{file_id, transaction_id}` and links each receipt. | A result per link: linked, or why not. |

How approval and safety work, as for `book`:

- `link_receipts` writes only what the user approved. Its description says so, and Claude Code asks permission for each call.
- It accepts only pairs from the latest `propose_receipts` in this server run: a receipt or transaction that wasn't proposed together is refused.
- Just before linking, it lists the files again and refuses a receipt that is no longer unlinked, so a receipt is never linked twice.
- A link request is never retried. After a timeout, the link comes back as "result unknown", and running `propose_receipts` again shows whether it was linked: a linked receipt is no longer unlinked.
- One failing link doesn't stop the others. An expired session stops the batch and says which links were not tried.

Claude shows the confident pairs as one table and the ambiguous ones one at a time, as in a quarter's session.

## Architecture

- `client` gains `files()` (all pages back to a given date) and `attach_file(file_id, transaction_id)`. `models` gains `File` with `id`, `created_at`, `name`, `mime_type`, `amount_cents | None` and `receipt_date | None` (from `metadata`), and `is_linked`. It ignores every other field, `secret` included.
- `Booking` gains the booking's `description`, for a split's targets.
- A new pure unit, `receipts`, matches targets against receipts and produces the proposal. It does no I/O.
- `server` adds the two tools.

## Testing

As before, no test talks to Hamurai, and fixtures are invented. The fake API gains the files list (with `offset` paging), the attach request, and files with and without `metadata`.

| Unit | Key cases |
|---|---|
| `receipts` | Exact amount in the window → confident; a day outside either window → no match; two receipts for one target, and one receipt for two targets → ambiguous; a split's lines are targets with the 45-day window; income, credits and transactions with files are not targets; receipts without an amount are counted. |
| `client` | `files()` pages by `offset` and stops at the date; `attach_file` sends exactly `{"transactionId": …}` and is never retried; a file's `secret` is not in the model. |
| `server` | `link_receipts` refuses unproposed pairs and receipts linked meanwhile before sending anything; one failure doesn't stop the rest; an expired session stops the batch; no tool output contains a file's `secret`. |

**After building, the first real run:** `propose_receipts` for Q3 2026 and Q2 2026, and the user checks any proposed link in the web app before approving it.

## Left to the builder

- Module layout and internal names.
- How the proposal is formatted for Claude.
