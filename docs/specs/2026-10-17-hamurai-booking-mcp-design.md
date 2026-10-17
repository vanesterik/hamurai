# Hamurai booking MCP: design

## Intent

Every quarter, every bank transaction in Hamurai has to be booked onto the right category ("rubriek", a general ledger account) and VAT code. Done by hand in the web app, this takes hours of repetitive clicking, and most of it is the same counterparties booked the same way as last quarter.

A custom MCP server does the work, with Claude Code as the interface. It remembers how each counterparty was booked before, proposes the same booking with its evidence, and books only what the user approves.

**Success:** a quarter's bookings take one Claude Code session instead of hours, and the result in Hamurai is the same as booking by hand.

## Goals

- Propose a category and VAT code for every unbooked bank transaction in a period, with the evidence behind each proposal.
- Book the approved proposals in Hamurai as manual bookings.
- Learn from every approved booking, so the next quarter needs fewer decisions.
- Make a mistaken booking easy to undo.

## Non-goals

- Hamurai's suggestions (its own booking guesses, from `reconciliation-suggestions`) are not trusted and not used. The word "suggestions" in this design always means *our* list, never Hamurai's.
- No user interface besides Claude Code.
- No logging in to Hamurai, and no refreshing the session automatically. The user supplies the session cookie.
- No handling of partly booked transactions. They are listed, never touched.
- The credit card split is not part of the first story (see "Second story").

## Anti-goals

These would make it a failure even if it technically works:

- **Anything booked without the user's approval.**
- **The session cookie leaking**: into logs, errors, tool output or the repository.
- **Bookkeeping data in the repository**: no administration ID, journal ID, IBANs, counterparty names or real transactions, not even in test fixtures.
- **A transaction booked twice.**

## Constraints

- Python, managed with `uv`. The official `mcp` SDK (FastMCP), `httpx` and Pydantic.
- Configuration comes only from environment variables: `HAMURAI_COOKIE`, `HAMURAI_ADMINISTRATION_ID`, `HAMURAI_JOURNAL_ID`. `HAMURAI_COOKIE` holds the whole `Cookie` request header (`name=value`, as copied from the browser), not just the cookie's value: a bare value is answered with `401`. `.env.example` says so. The user decided on the environment variable for the cookie as the least complex option. An in-app prompt for the cookie (a macOS dialog plus Keychain) was considered and rejected.
- The list of learned bookings lives at `data/suggestions.yaml` inside the repository, and `data/` is in `.gitignore`.
- The Hamurai API is not public. It was reverse engineered from the web app (see "The Hamurai API").
- No CI. Checks run locally before every commit.

## Architecture

Six units, each with one job:

| Unit | Job | Depends on |
|---|---|---|
| `config` | Reads the three environment variables. Fails immediately with a clear message if any are missing. | nothing |
| `client` | The only unit that talks to Hamurai over HTTP: list transactions (with paging), categories, VAT codes, create a booking, delete a booking. Turns a `401` into `SessionExpired`. | `config`, `httpx` |
| `models` | Pydantic models with only the fields that are used, ignoring any others. | `pydantic` |
| `suggestions` | Reads, writes and seeds `data/suggestions.yaml`: one entry per counterparty, with category, VAT code, how often it was booked that way, and when it was last seen. | `models` |
| `matcher` | Pure logic with no I/O: a transaction plus the suggestions gives a proposal with a confidence level and its evidence. | `models`, `suggestions` |
| `server` | The MCP tools. Connects the units together and holds no logic of its own. | everything |

Principles:

- Only `client` touches the network, and only `suggestions` touches the file.
- The cookie never appears in logs, errors or tool output.
- Every booking is sent as a manual booking (`suggestionAccepted: false`) with an explicit `vatCodeId`.
- The credit card split adds a `statement` unit, which reads the card statement, and an update call in `client`. The units above change only where the split needs them to (see "Second story").

## Matching

The matcher looks for a transaction's counterparty in the suggestions in this order: IBAN (`counterAccount.number`), then counterparty name (`counterAccount.nameAccountHolder`), then patterns in the description. It returns one of three confidence levels:

| Level | When |
|---|---|
| `confident` | An IBAN match. |
| `uncertain` | A match on name or description only, **or** any match on a counterparty marked *always ask*. |
| `unknown` | No match at all. |

**The latest choice wins.** Each entry has a current category and VAT code, and that is what the matcher proposes. When the user approves a booking that differs from it, the entry switches to the new pair, and the next quarter proposes the new pair as `confident`. The older counts are kept for the evidence, so a changed counterparty reads like "IBAN match, now 4600 / 21% VAT (1×, since 2026-10-17); before that 14× 4500". Mixed histories from before seeding reach the matcher only as *always ask* counterparties (see below), because the user resolves them during seeding. *(The user's decision. It replaces the earlier rule that a mixed history counts as `uncertain`.)*

An entry in the suggestions holds its current category and VAT code, plus a count per pair it has ever been booked to, so a change stays visible. It also keeps the booking IDs it learned from, so an undo knows which count to take back, even after the server has been reconnected. *(Builder's call.)*

Description patterns are only written by hand in the YAML. Nothing learns them automatically. A transaction with neither an IBAN nor a name, such as a bank fee, is booked but not learned, unless a hand-written pattern matches it. *(Builder's call.)*

Each proposal carries its evidence in words, e.g. "IBAN match, 14× booked as 4500 / 21% VAT, last seen 2026-07-03". For a mixed history the evidence names each category and its count.

Claude uses its own judgment only on `unknown` transactions, by picking a category and VAT code from Hamurai's lists.

## Data flow

### Seeding (once)

1. `client` fetches the full transaction history, page by page.
2. `suggestions` groups the booked transactions by counterparty (IBAN first, then name) and records category, VAT code, count and last-seen date per group.
3. Counterparties booked under more than one category or VAT code are flagged.
4. The user reviews a summary once and resolves each flagged counterparty in one of two ways. Only then is `data/suggestions.yaml` written.
   - **One pair:** one category and VAT code from its history. The other counts for that counterparty are dropped, so it starts out `confident`. *(Builder's call.)*
   - **Always ask:** for a counterparty with no single right category, such as the user's own account (salary and current-account transfers), the tax office or payment requests. Its whole history is kept and it is marked `always_ask`: it is always proposed as `uncertain`, with the latest pair as the proposal and the whole history as evidence, so it is never part of the confident table. Learning keeps the mark. *(The user's decision, after seeding the real history flagged 47 counterparties, about 14 of them of this kind.)*

   For each flagged pair, the summary gives its count and when it was last used, so a VAT rate that changed over time (6% to 9%, or 21% to reverse charge) can be decided on the current one.

A transaction split over more than one booking (`numberOfReconciliations > 1`) is left out of seeding. *(Builder's call.)*

After seeding, the suggestions only grow through bookings the user approves.

### A quarter's session

1. Claude asks for the quarter's transactions. `client` fetches the date range, and fully booked transactions are left out.
2. `matcher` puts each remaining transaction in `confident`, `uncertain` or `unknown`, with its evidence. Partly booked transactions (`numberOfReconciliations > 0` but not `isFullyReconciled`) go in a separate `skipped` group.
3. Claude shows all the confident proposals as one table. The user approves it or strikes lines from it. Only the remaining lines are booked.
4. The uncertain and unknown transactions are handled one at a time. Claude proposes, the user approves or corrects.
5. Every approved booking updates the suggestions: count up, last-seen date moved, new counterparties added.
6. Every booking ID created in the session is kept, so a mistake can be undone with a delete.

## MCP tools

| Tool | What it does | Returns |
|---|---|---|
| `seed_suggestions()` | Reads the full history and builds the suggestions **without writing them**. | A summary per counterparty, with the inconsistent ones flagged. |
| `save_suggestions(resolutions)` | Writes `data/suggestions.yaml`, using the user's decision for each flagged counterparty: `{key, category_id, vat_code_id}` or `{key, always_ask: true}`. | How many entries were written. |
| `list_categories()`, `list_vat_codes()` | Lets Claude look up IDs and names, for transactions with no match. | ID, code, name. |
| `propose_bookings(start_date, end_date)` | Fetches the period and runs the matcher. **Writes nothing.** | Four groups (`confident`, `uncertain`, `unknown`, `skipped`). Each line has transaction ID, date, amount, description, counterparty, proposed category and VAT code, and evidence. |
| `book(bookings)` | Takes a list of `{transaction_id, category_id, vat_code_id}`. Books each line as a manual booking and updates the suggestions for each one that succeeds. | A result per line: the booking ID, or why it failed. |
| `undo_booking(booking_id)` | Deletes the booking and takes back the count it added to the suggestions. | Confirmation. |

How approval is guaranteed:

- `book` is the only tool that writes to Hamurai. Its description tells Claude to call it only after the user has explicitly approved the lines.
- Claude Code asks permission for every `book` call unless the user allows it permanently. That's a hard stop outside Claude's own judgment.
- `book` takes explicit IDs rather than a reference to a proposal, so a corrected line is booked exactly as corrected.
- `book` takes a list: the confident table goes in one call, each one-by-one decision in its own.
- One failing line doesn't stop the rest, except for an expired session (see below).
- `book` accepts only transactions that came out of a `propose_bookings` call in the same server run. That's where it gets the IBAN, name and date it needs for learning. After a reconnect, `propose_bookings` has to run again first. *(Builder's call.)*
- Just before it sends anything, `book` fetches the date range of its lines again and refuses any transaction that is already booked. This guards against booking twice, whether or not Hamurai would refuse a second booking itself. *(Builder's call.)*

## Error handling

**At startup**

- A missing environment variable stops the server. The message names the variable, never its value.
- A missing `data/suggestions.yaml` is not an error. Everything comes back as `unknown`, with a hint to run `seed_suggestions` first.
- A corrupt `data/suggestions.yaml` stops the server with the line number. The file is never overwritten in that state.
- The file is written atomically (to a temporary file, then renamed), so a crash can't leave a half-written file.

**An expired session** (`401` with error code `2003`)

- Every tool answers with one clear message: the session has expired, update `HAMURAI_COOKIE` and reconnect the server (`/mcp`).
- If it happens partway through a `book` batch, the batch stops. The answer lists which lines were booked and which weren't tried.

**Booking**

- `book` checks every category ID and VAT code ID against Hamurai's lists before it sends anything. A typo fails before the network is touched.
- Any other error from Hamurai fails that line, with the status and Hamurai's message. The other lines go on.
- A booking request is **never retried automatically**. After a timeout the line comes back as "result unknown". Running `propose_bookings` again shows whether it was booked, and the check in `book` refuses it if it was, so nothing gets booked twice.
- Read requests (lists, transactions) are retried once after a network error.

**The cookie**

- Error messages are built from our own text plus Hamurai's response status and body. Request headers, and raw exception objects that might contain them, are never passed on.

## Testing

No test talks to the real Hamurai. Fixtures are made-up JSON shaped like the real responses, with invented IBANs and names.

| Unit | How | Key cases |
|---|---|---|
| `matcher` | Plain unit tests, one table of cases | IBAN match → `confident` with the entry's current pair; name or description match only → `uncertain`; no match → `unknown`; the evidence text, including for a changed counterparty. |
| `suggestions` | Against a temporary directory | Seeding groups by IBAN, then name; inconsistent counterparties are flagged; a booking increments the count and undo takes it back; a booking to a different pair makes it the current one, and undoing that booking switches back; a missing file gives empty suggestions; a corrupt file gives an error with the line number and stays untouched. |
| `client` | `httpx.MockTransport`, no network | Paging stops at `page.last`; `401`/`2003` → `SessionExpired`; the exact POST body (`suggestionAccepted: false`, always a `vatCodeId`); DELETE uses the booking ID; reads are retried once, POSTs never. |
| `config` | Unit tests | A missing variable gives an error that names it and doesn't contain its value. |
| `server` | Tools called in-process, with a fake client | `book` rejects unknown IDs before sending anything; one failing line doesn't stop the others; `SessionExpired` stops the batch and reports what was done; the cookie value appears in no tool output, including errors. |

Tooling (the builder's call, recorded here): `pytest`, `ruff` with the `ANN` rules on, and `ty` for type checking. All three run locally before every commit.

**After building, the first real run:** book a handful of transactions and check them in the Hamurai web app against what the user would have booked by hand.

## The Hamurai API

Reverse engineered from the web app. Base URL `https://app.hamurai.example`. Authentication is the browser session cookie (`Authorization=Bearer…`), plus two fixed headers the web app sends on every request: `x-hamurai: HAMURAI` and `device-type: DESKTOP`. Reads work without those two headers; bookings are refused without them, with "You have no access to this action." (found on the first live run, 2026-10-17). A session lasts about a day. When it expires, the API answers `401` with error code `2003`.

- `GET /api/v2/administrations/{admin}/transactions?journalId=&startDate=&endDate=&page=&size=` returns `{content: [...], page: {pageNumber, pageSize, totalPages, totalElements, first, last}}`. The dates are optional. `page` counts from 0. A size of 5000 was accepted in testing, but the client loops over pages of about 500 until `page.last` is true. Each transaction includes `id`, `entryDate`, `amount` (an integer in **cents**, equal to the booking's `amountInCents`), `description`, `counterAccount {number, nameAccountHolder}`, `isFullyReconciled`, `numberOfReconciliations`, `reconciliationInfo` and `hasSuggestions`. The list is sorted newest first. For a booked transaction, `reconciliationInfo.title` is the category's name and `reconciliationInfo.subtitle` contains the VAT code's description: useful for showing, but names aren't IDs, so it isn't used for seeding.
- `GET /api/v2/administrations/{admin}/transactions/{id}` returns one transaction, in the same shape as in the list.
- `GET /api/v2/administrations/{admin}/reconciliations?transactionId={id}` returns a list of the transaction's bookings. Each has its own `id`, `type`, `transactionInfo`, `generalLedgerAccountInfo {id, type, name}`, `vatCode {id, percentage, description, isReverseChargeCode, ...}`, `amountInCents` and created/updated metadata. Only one `transactionId` per request is honoured. No bulk form was found: without `transactionId`, and with `startDate`/`endDate`, `journalId`, `page`/`size` or `transactionIds`, the endpoint answers `400`.
- `GET /api/v2/administrations/{admin}/general-ledger-accounts` returns the categories.
- `GET /api/v2/administrations/{admin}/vat-codes` returns the VAT codes.
- `GET /api/v2/administrations/{admin}/reconciliation-suggestions?...` returns Hamurai's suggestions. Not used.
- `POST /api/v2/administrations/{admin}/reconciliations` creates a booking. Body: `{"type": "GENERAL_LEDGER_ACCOUNT", "transactionId", "generalLedgerAccountId", "vatCodeId", "suggestion": null, "suggestionAccepted": false}`. Always send `vatCodeId`: a booking made without one came back as "Geen / Vrijgesteld" (no VAT). The response includes the booking's own `id`, `generalLedgerAccountInfo`, `vatCode`, `amountInCents`, and created/updated metadata.
- `DELETE /api/v2/administrations/{admin}/reconciliations/{bookingId}` answers `204`. It takes the booking `id`, not the transaction ID.

## Seeding reads one transaction at a time

A read-only check against the live API (2026-10-17) settled this. The exact category and VAT code IDs of a booked transaction are only available from `reconciliations?transactionId=`, one call per transaction. The history holds about 3,400 transactions. In the first 500, about 80% were fully booked and about 3% of those were split. So seeding makes in the order of 2,500–3,000 GET requests.

That's acceptable for something that runs once. `seed_suggestions` runs those calls with a small fixed concurrency (about 4 at a time) and reports progress. A `401` partway through stops seeding without writing anything. *(Builder's call.)* Split transactions are skipped from the list's `numberOfReconciliations` before any call is made.

## Second story: the credit card split

Issue #3. The card company's monthly collection shows up on the bank account as one transaction. In the user's words, it is one transaction made of several smaller ones: the lines of the card statement. Each line is matched and booked like any other transaction, against the same suggestions. Only the flow differs: the lines come from a file, and together they book one bank transaction.

### The statement

The user downloads the statement as a CSV from the bank that issues the card, and drags it into the Claude Code prompt, which pastes its path. The tool reads the file itself. *(The user's decision: no copying into `data/`, which stays one place a statement may be kept.)* The file is parsed in code, not passed through the conversation, so the amounts arrive exactly and the card's identifying columns never reach tool output.

The columns are `Accountnummer, Kaartnummer, Naam op kaart, Transactiedatum, Boekingsdatum, Omschrijving, Valuta, Bedrag, Koers, Bedrag in EUR`.

- A line is its `Transactiedatum`, `Omschrijving` and `Bedrag in EUR`. Foreign-currency rows are already converted, so `Valuta`, `Bedrag` and `Koers` are not used.
- `Accountnummer`, `Kaartnummer` and `Naam op kaart` are read past and never kept, returned or logged.
- The file is comma-separated UTF-8 without a byte order mark, with CRLF line ends and quoted fields. Blank lines occur (one follows the first row) and are skipped. Dates are `YYYY-MM-DD`. *(Checked on a real statement, 2026-10-17, by its format only.)*
- Amounts use a decimal comma (`"-12,99"`) and are turned into integer cents without going through a float. Purchases are negative. A thousands dot (`"-1.234,56"`) wasn't seen but is accepted. *(Builder's call.)*
- The account number field starts with `'`.
- **The card payment row is left out.** The statement opens with the previous collection arriving on the card account, as a positive row described as `BETALING ONTVANGEN - DANK U`. It isn't a purchase: the purchases alone add up to the collection. Rows whose `Omschrijving` starts with `BETALING ONTVANGEN` are left out of the lines and listed as left out.
- **A refunded purchase is left out, together with its refund.** Sometimes a purchase is followed by a refund for the same amount, and the two cancel out. Neither is booked: there is no use in keeping track of costs that were refunded *(the user's decision)*. A refund is paired with a purchase of exactly the opposite amount, preferring one with the same `Omschrijving`. Both are listed as left out. *(The pairing is the builder's call.)*
- Any other positive row is refused, with the whole statement: a refund without a matching purchase in the same file (a partial refund, or one for an earlier statement), or one whose candidate purchases come from more than one merchant, none with the refund's own `Omschrijving`. A refund is never booked by a guess.
- A file without these columns is refused with a message naming the missing ones, never quoting a row.

**One file is one collection.** All the lines of the file together make up one collection. A statement covers a period by `Boekingsdatum`, and the user exports one statement per file. *(Builder's call.)*

### Finding the collection

The total of the lines, in cents, is the amount of the collection. The lines are the rows left once the payment row and any refunded pairs are left out. The tool fetches the bank transactions from the earliest `Boekingsdatum` to 45 days after the latest *(builder's call)* and looks for an unbooked transaction (`numberOfReconciliations` 0) with exactly that amount.

- One match: that's the collection.
- No match: refused, with the file's total, and nothing is booked. This includes a collection that is already partly or fully booked: partly booked transactions are still never touched.
- More than one match: refused, listing the candidates by date and description.

**The first story's tools are left alone.** Until it is split, the collection is an ordinary unbooked transaction, so `propose_bookings` lists it. The card company is already marked *always ask* in the suggestions, so the collection is always `uncertain` and never part of the confident table: Claude points to `propose_split` for it instead of proposing one booking. *(Builder's call.)*

### Matching a line

A line is matched as a transaction without an IBAN: its counterparty name is the `Omschrijving`, normalised the way names are now, and then the patterns are tried against the `Omschrijving`.

**A name match on a card line is `confident`**, unlike a name match on a bank transaction, because for a card line the merchant is the only key there is. *Always ask* still makes it `uncertain`, and a pattern match stays `uncertain`. *(Builder's call.)*

For an unknown merchant Claude proposes the category, and the VAT code by **the user's rule: a merchant located outside the EU gets "Geen", one inside the EU gets "Hoog 21%"**, judged from the description. The tool's description tells Claude this rule.

### Proposing and booking

The two new tools follow the first story's approval rules: one writes nothing, the other writes only what the user approved.

1. `propose_split(statement_path)` reads the file, finds the collection and matches each line. **It writes nothing.** It returns the collection (ID, date, amount, description) and the lines, numbered in file order, each with date, description, amount, confidence, proposed category and VAT code, and evidence.
2. Claude shows the confident lines as one table and goes through the uncertain and unknown ones one at a time, as in a quarter's session. A split is all or nothing, so a struck line is corrected rather than left out.
3. `book_split(transaction_id, lines)` takes the collection's ID and every line exactly once, as `{line, category_id, vat_code_id}`. Before it sends anything it checks that:
   - the collection and its lines come from the latest `propose_split` in this server run (the lines are kept from that call; the file is not read again),
   - every line is listed exactly once,
   - every category and VAT code ID is in Hamurai's lists,
   - the collection, fetched again, is still unbooked.
4. For each line, in file order, it sends two requests, as the web app does:
   1. **Create**: the existing booking request, with the line's category and VAT code. Hamurai gives the new booking the collection's whole unbooked remainder.
   2. **Update**: the line's amount in cents, the category and VAT code again, the line's `Omschrijving` as the booking's description *(the user's decision, so each booking names its merchant in Hamurai)*, and the collection's date as the entry date, as when splitting by hand.
5. Once every line is booked, each one is learned under its merchant name, with the line's `Transactiedatum` as the date. Nothing is learned from a split that didn't complete.

**All or nothing.** A split that stops partway leaves a booking holding the whole remainder under one category, which makes the collection look fully booked. So when any request fails, `book_split` stops, deletes every booking it created in this call (including one created but not yet updated), and reports what happened.

- If a delete fails too, or the session has expired, the answer lists the booking IDs left behind, to be removed with `undo_booking` or in the web app.
- A booking request is never retried. When a create gets no answer, its booking may or may not exist: the known bookings are deleted, and the answer says to check the collection in Hamurai, where `propose_bookings` lists it as `skipped` if something was left.

The answer lists each line's booking ID. `undo_booking` works on them as it does now, one booking at a time, and undoing a whole split means undoing each of its bookings.

### What the split adds to the API

Recorded from the web app on 2026-10-17, splitting a real collection by hand into five lines:

- `POST /api/v2/administrations/{admin}/reconciliations` (the existing create) on an unbooked or partly booked transaction gives the new booking the transaction's whole unbooked remainder.
- `POST /api/v2/administrations/{admin}/reconciliations/{bookingId}` updates a booking. Body: `{"amount", "generalLedgerAccountId", "vatCodeId", "description", "entryDate"}`, with `amount` a signed integer in **cents** (`-1299` for −€12,99) and `entryDate` as `YYYY-MM-DD`. It answers `200` with the booking, in the same shape as the create's answer, with `amountInCents` set to the new amount.
- A booking made without `vatCodeId` got the category's default VAT code, which confirms that the create must always send one.

### Testing the split

Fixtures are invented: made-up merchants, card and account numbers, never rows from a real statement.

| Unit | Key cases |
|---|---|
| `statement` | Decimal comma to cents, with and without a thousands dot; the leading `'`; blank lines skipped; the payment row left out; a purchase and its refund left out as a pair, preferring the same description; a refund with no matching purchase, or with several merchants to choose from, refused; a missing column refused by name; the card number, account number and name on the card appear in no parsed line. |
| `client` | The exact update body, with the amount in cents; the update is never retried. |
| `matcher` | A card line's name match is `confident`; *always ask* and patterns stay `uncertain`. |
| `server` | No matching collection, or several, is refused before anything is sent; a line listed twice or missing is refused; a failure between a create and its update deletes everything the call created; nothing is learned from an incomplete split; the card number appears in no tool output, including errors. |

### Not in this story

- Linking receipts (#4).
- Learning merchants from collections split by hand earlier: those bookings carry the bank's text, not the merchant's.
- A file holding more than one statement, or booking only part of a collection.
- Matching payment-provider IBANs by merchant name (#2).

## Left to the builder

- Module and package layout, and the names of internal functions.
- The exact shape of the YAML file, as long as it holds category, VAT code, count and last-seen date per counterparty and stays readable by hand.
- How description patterns are stored and matched.
- How the tool output is formatted for Claude.
- Page size for the transaction list, around 500.
