# Credit card split implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, in this session, on the `card-split` branch. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two MCP tools that split a credit card collection into one booking per line of the card statement: `propose_split` reads the statement and proposes, and `book_split` books the approved lines all or nothing.

**Architecture:** A new pure `statement` unit reads the CSV into lines, leaving out the card payment and refunded pairs. `client` gains the update call. `matcher` proposes for a line by merchant name, and `suggestions` learns a line without a transaction. `server` connects them in two tools, keeping the proposed split in its state, as `book` does with `propose_bookings`.

**Tech Stack:** Python 3.14, `uv`, `mcp` (`MCPServer`), `httpx`, `pydantic` 2, `csv` from the standard library; `pytest` with `anyio`, `ruff`, `ty`.

**Spec:** `docs/specs/2026-10-17-hamurai-booking-mcp-design.md`, section "Second story: the credit card split".

## Global Constraints

- No real data in the repository, fixtures included: invented merchants, card numbers (`5555 99** **** 0001`), account numbers (`'00000000001`) and names.
- `Accountnummer`, `Kaartnummer` and `Naam op kaart` are never kept, returned, logged or put in an error. Errors about the statement name a row number or a column, never a value.
- Amounts are integer cents everywhere, parsed without a float.
- Only `client` touches the network and only `suggestions` touches `data/suggestions.yaml`. `statement` only reads the file it is given.
- A booking request (create or update) is never retried. Every booking is created with an explicit `vatCodeId`.
- Nothing is written to Hamurai without the user's explicit approval: `propose_split` is read-only, and `book_split`'s description says to call it only after approval.
- Before each commit: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run ty check` all pass. There is no CI.
- Commits: Conventional Commits, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Check the branch in the same command as each commit and push, and push with an explicit `card-split:card-split` refspec.

## Review Focus

1. **A dragged-in path.** Dragging a file from Finder into the terminal can give `/Users/x/My\ Statements/card.csv`, or the path in quotes. Both must work, as must `~/Desktop/card.csv`. Task 4, test `test_propose_split_accepts_a_dragged_path`.
2. **`book_split` called again after it succeeded**, for example by a repeated approval, must be refused without sending anything. Task 5, test `test_book_split_twice_is_refused`.
3. **The collection already booked by hand** (or partly) must give "no unbooked collection", not a proposal. Task 4, test `test_propose_split_skips_a_booked_collection`.
4. **A merchant written with different case or spacing** than when it was learned (`SPOTIFY  P0001` vs `Spotify P0001`) must still match. Task 3, test `test_line_name_match_ignores_case_and_spacing`.
5. **A statement with nothing left to book**: header only, or only the payment row. It must be refused clearly rather than "matching" a zero-amount transaction. Task 1, test `test_statement_without_purchases_is_refused`.

---

### Task 1: The `statement` unit

**Files:**
- Create: `src/hamurai/statement.py`, `tests/test_statement.py`

**Interfaces:**
- Produces:
  - `class StatementError(Exception)`.
  - `class StatementLine(BaseModel, frozen=True)`: `number: int` (1-based, in file order, counting only the lines kept), `transaction_date: date`, `booking_date: date`, `description: str`, `amount_cents: int`.
  - `class LeftOut(BaseModel, frozen=True)`: `transaction_date: date`, `description: str`, `amount_cents: int`, `reason: Literal["card payment", "refunded", "refund"]`.
  - `class Statement(BaseModel)`: `lines: list[StatementLine]`, `left_out: list[LeftOut]`, and the properties `total_cents: int` (sum of the lines), `first_booking_date: date` and `last_booking_date: date` (over the lines).
  - `parse_amount(text: str) -> int`: `"-12,99"` → `-1299`, `"-1.234,56"` → `-123456`, `"293,50"` → `29350`. Raises `ValueError`.
  - `read_statement(path: Path) -> Statement`. Raises `StatementError`.
  - `PAYMENT_PREFIX = "BETALING ONTVANGEN"`.

- [ ] **Step 1: Write the failing tests in `tests/test_statement.py`**

Build files with a helper `write_statement(tmp_path, rows: list[tuple[str, str, str, str]], blank_after_first: bool = True) -> Path` taking `(Transactiedatum, Boekingsdatum, Omschrijving, Bedrag in EUR)`. It writes the real header, CRLF line ends, every field quoted, invented values for the other columns (`'00000000001`, `5555 99** **** 0001`, `J JANSEN`, `EUR`, the same amount, empty `Koers`), and one blank line after the first row, as in the real file.

```python
def test_parse_amount():
    assert parse_amount("-12,99") == -1299
    assert parse_amount("-1.234,56") == -123456
    assert parse_amount("293,50") == 29350
    assert parse_amount("-5,00") == -500
    with pytest.raises(ValueError): parse_amount("12.99")      # a dot alone is not a decimal
    with pytest.raises(ValueError): parse_amount("")

def test_reads_lines_and_leaves_out_the_payment(tmp_path):
    # rows: payment "BETALING ONTVANGEN - DANK U" +293,50, then purchases
    # ("2026-08-30","2026-08-31","ACME SOFTWARE","-12,99"), ("2026-08-24","2026-08-25","EXAMPLE CLOUD","-84,62")
    statement = read_statement(path)
    assert [(l.number, l.description, l.amount_cents) for l in statement.lines] == [
        (1, "ACME SOFTWARE", -1299), (2, "EXAMPLE CLOUD", -8462)]
    assert statement.lines[0].transaction_date == date(2026, 8, 30)
    assert statement.lines[0].booking_date == date(2026, 8, 31)
    assert [(o.reason, o.amount_cents) for o in statement.left_out] == [("card payment", 29350)]
    assert statement.total_cents == -9761
    assert statement.first_booking_date == date(2026, 8, 25)
    assert statement.last_booking_date == date(2026, 8, 31)

def test_identifying_columns_are_not_kept(tmp_path):
    # dump every model of the statement to JSON: "5555", "00000000001" and "JANSEN" appear nowhere

def test_refund_is_left_out_with_its_purchase(tmp_path):
    # purchase "SHOP A" -20,00, purchase "SHOP B" -5,00, refund "SHOP A" +20,00
    # → lines == [SHOP B -500]; left_out reasons == ["refunded", "refund"] for SHOP A

def test_refund_prefers_the_purchase_with_its_description(tmp_path):
    # purchases "SHOP A" -20,00 and "SHOP B" -20,00, refund "SHOP B" +20,00 → SHOP A is the line left

def test_refund_pairs_within_one_merchant(tmp_path):
    # purchases "SHOP A" -20,00 twice, refund "SHOP A REFUND" +20,00 → one SHOP A line left

def test_refund_without_its_purchase_is_refused(tmp_path):
    # refund +7,50 with no -7,50 purchase → StatementError mentioning "row" and not "7,50"

def test_ambiguous_refund_is_refused(tmp_path):
    # purchases "SHOP A" -20,00 and "SHOP B" -20,00, refund "SHOP C" +20,00 → StatementError

def test_missing_column_is_named(tmp_path):
    # header without "Bedrag in EUR" → StatementError matching "Bedrag in EUR"

def test_bad_amount_names_the_row_not_the_value(tmp_path):
    # Bedrag in EUR "abc" on the second data row → message contains "row 2", not "abc"

def test_statement_without_purchases_is_refused(tmp_path):
    # only the payment row → StatementError matching "no purchases"; header only → the same

def test_unreadable_file_is_refused(tmp_path):
    # a missing path → StatementError naming the file; cp1252 bytes (b"\xe9") → StatementError
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_statement.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'hamurai.statement'`

- [ ] **Step 3: Implement `src/hamurai/statement.py`**

Read with `path.read_text(encoding="utf-8-sig")` and `csv.DictReader(io.StringIO(text, newline=""))`. Skip rows whose fields are all empty. Leave out a positive row whose `Omschrijving` starts with `PAYMENT_PREFIX` (case-insensitive) as `"card payment"`. Error messages count data rows from 1, blank lines not counted. Pair refunds after reading, in file order:

```
for each other positive row R:
    candidates = unpaired purchases with amount == -R.amount
    if none: refuse ("row N: a refund without a purchase of the same amount")
    same = [c for c in candidates if normalize_name(c.description) == normalize_name(R.description)]
    if same: pick same[0]
    elif len({normalize_name(c.description) for c in candidates}) == 1: pick candidates[0]
    else: refuse ("row N: a refund that matches purchases from more than one merchant")
    left_out += [pick as "refunded", R as "refund"]
```

Use `normalize_name` from `hamurai.suggestions`. Number the kept lines 1..n afterwards. No lines left → `StatementError("the statement has no purchases to book")`.

- [ ] **Step 4: Run them to see them pass**

Run: `uv run pytest tests/test_statement.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

`feat: read the card statement into lines`, after the full check from the Global Constraints.

---

### Task 2: The update call in `client`

**Files:**
- Modify: `src/hamurai/client.py`, `tests/fake_api.py`, `tests/test_client.py`

**Interfaces:**
- Produces: `HamuraiClient.update_booking(booking_id: int, transaction_id: int, *, amount_cents: int, category_id: int, vat_code_id: int, description: str, entry_date: date) -> Booking`. Posts to `/reconciliations/{booking_id}` with exactly `{"amount", "generalLedgerAccountId", "vatCodeId", "description", "entryDate"}` (`entryDate` as `YYYY-MM-DD`). Error handling as in `create_booking`: `ConnectError` → `HamuraiError(0, ...)`, any other `TransportError` or an unreadable answer → `BookingOutcomeUnknown`, never retried. `transaction_id` fills `transactionInfo` if the answer lacks it, as in `create_booking`.
- Produces in the fake: a create gives the new booking the transaction's unbooked remainder (its amount minus the bookings already on it). `POST /reconciliations/{id}` updates `amountInCents`, the category, the VAT code, `description` and `entryDate`, and answers `200` with the booking. A transaction is fully reconciled when its bookings add up to its amount. `add_booking` keeps working as before for the existing tests. Three new failure lists, each consumed one entry per request on its own route, where `None` lets that request through: `fail_creates` (`POST /reconciliations`), `fail_updates` (`POST /reconciliations/{id}`) and `fail_deletes` (`DELETE /reconciliations/{id}`), each `list[Exception | int | None]`.

- [ ] **Step 1: Write the failing tests in `tests/test_client.py`**

```python
async def test_update_booking_sends_the_recorded_body(client, fake):
    # transaction 1 of -9761; booking = await client.create_booking(1, 10, 20)
    # updated = await client.update_booking(booking.id, 1, amount_cents=-1299, category_id=10,
    #     vat_code_id=21, description="ACME SOFTWARE", entry_date=date(2026, 9, 21))
    assert json.loads(fake.requests[-1].content) == {
        "amount": -1299, "generalLedgerAccountId": 10, "vatCodeId": 21,
        "description": "ACME SOFTWARE", "entryDate": "2026-09-21"}
    assert fake.requests[-1].url.path.endswith(f"/reconciliations/{booking.id}")
    assert (updated.amount_cents, updated.vat_code_id, updated.transaction_id) == (-1299, 21, 1)

async def test_create_takes_the_remainder(client, fake):
    # after the update above, a second create_booking(1, 10, 20) has amount_cents == -8462;
    # the transaction is fully reconciled only once the bookings add up to -9761

async def test_update_booking_is_never_retried(client, fake):
    # fake.fail_updates = [httpx.ReadTimeout("timeout")]
    # → BookingOutcomeUnknown, and only one update request was sent
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_client.py -v`
Expected: FAIL, `AttributeError: 'HamuraiClient' object has no attribute 'update_booking'`

- [ ] **Step 3: Implement the fake's remainder and update, then `update_booking`**

- [ ] **Step 4: Run all tests to see them pass**

Run: `uv run pytest -v`
Expected: PASS, the existing booking tests included.

- [ ] **Step 5: Commit**

`feat: update a booking's amount, for splitting a transaction`.

---

### Task 3: Matching and learning a card line

**Files:**
- Modify: `src/hamurai/matcher.py`, `src/hamurai/suggestions.py`, `tests/test_matcher.py`, `tests/test_suggestions.py`

**Interfaces:**
- Consumes: `StatementLine` (Task 1).
- Produces:
  - `class LineProposal(BaseModel)`: `line: StatementLine`, `confidence: Confidence`, `category_id: int | None`, `vat_code_id: int | None`, `evidence: str`.
  - `propose_line(line: StatementLine, suggestions: Suggestions, labels: Labels) -> LineProposal`: a name match on `line.description` is `confident` with the evidence starting `"Merchant match"`; *always ask* gives `uncertain`, as `_from_entry` does now; then the patterns against the description (`uncertain`); otherwise `unknown`. Share the evidence-building code with `propose`, so the wording stays the same.
  - `Suggestions.learn(*, iban: str | None, name: str | None, booking: Booking, on: date) -> bool`: what `record` does now, without a transaction. `record(transaction, booking)` becomes a call to `learn`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_matcher.py
def test_line_name_match_is_confident():
    # entry name "ACME SOFTWARE", one booking 10/20 → confident, (10, 20), evidence.startswith("Merchant match")

def test_line_name_match_ignores_case_and_spacing():
    # entry name "Acme Software", line description "ACME  SOFTWARE" → confident

def test_line_always_ask_is_uncertain(): ...
def test_line_pattern_is_uncertain():
    # pattern contains "cloud" → uncertain with the pattern's pair
def test_line_without_a_match_is_unknown(): ...

# tests/test_suggestions.py
def test_learn_by_name_only():
    # learn(iban=None, name="ACME SOFTWARE", booking=<booking 7: 10/20>, on=date(2026, 8, 30))
    # → True; by_name("acme software").bookings == [LearnedBooking(id=7, ..., date=date(2026, 8, 30))]
    # and forget(7) takes it back
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_matcher.py tests/test_suggestions.py -v`
Expected: FAIL, `ImportError: cannot import name 'propose_line'`

- [ ] **Step 3: Implement `learn`, then `propose_line`**

- [ ] **Step 4: Run all tests to see them pass**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

`feat: match and learn a card line by its merchant`.

---

### Task 4: `propose_split`

**Files:**
- Modify: `src/hamurai/server.py`
- Create: `tests/test_server_split.py`

**Interfaces:**
- Consumes: `read_statement`, `Statement`, `LeftOut`, `StatementError` (Task 1); `propose_line`, `LineProposal` (Task 3).
- Produces:
  - `class SplitProposal(BaseModel)`: `collection: Transaction`, `lines: list[LineProposal]`, `left_out: list[LeftOut]`, `total_cents: int`.
  - `State.split: tuple[Transaction, Statement] | None`, set by every successful `propose_split` and cleared by a failed one.
  - `COLLECTION_WINDOW = timedelta(days=45)`.
  - The tool `propose_split(statement_path: str) -> SplitProposal`, with `READ_ONLY`.
  - `statement_file(text: str) -> Path`: strips surrounding quotes and whitespace, unescapes `\ `, expands `~`.

The tool's description says: it writes nothing; amounts are in cents; for an unknown merchant, propose the category and choose the VAT code by the user's rule, "Geen / Vrijgesteld" for a merchant located outside the EU and the 21% code for one inside the EU; show the confident lines as one table and the rest one at a time; every line must be booked, so correct a line rather than leave it out. `propose_bookings`' description gains one sentence: a credit card collection is split with `propose_split`, not booked as one line.

- [ ] **Step 1: Write the failing tests in `tests/test_server_split.py`**

Reuse `connected` and `text` from `test_server_read`, and `write_statement` from `test_statement`. The fixture writes a statement with the payment row and two purchases totalling -9761, and adds collection `make_transaction(50, iban=None, name="Card Company", amount=-9761, entry_date="2026-09-21")`.

```python
async def test_propose_split_finds_the_collection_and_proposes(client, fake, path, statement):
    # suggestions know "ACME SOFTWARE" → 10/20
    result = await session.call_tool("propose_split", {"statement_path": str(statement)})
    data = result.structured_content
    assert data["collection"]["id"] == 50 and data["total_cents"] == -9761
    assert [(l["line"]["number"], l["confidence"]) for l in data["lines"]] == [(1, "confident"), (2, "unknown")]
    assert [o["reason"] for o in data["left_out"]] == ["card payment"]
    assert not [r for r in fake.requests if r.method != "GET"]

async def test_propose_split_looks_45_days_past_the_last_booking_date(...):
    # the transactions request has startDate = first booking date, endDate = last + 45 days

async def test_propose_split_without_a_matching_collection(...):
    # collection amount -9760 → is_error; the message contains "-9761" and says nothing is booked

async def test_propose_split_skips_a_booked_collection(...):
    # fake.add_booking(50, 10, 20) first → is_error, the same "no unbooked collection" message

async def test_propose_split_with_two_candidates(...):
    # a second unbooked -9761 transaction → is_error listing both dates

async def test_propose_split_accepts_a_dragged_path(tmp_path, ...):
    # the statement in tmp_path / "My Statements" / "card.csv"; works given as
    # the plain path, the path with "\ " for spaces, and the path in single quotes

async def test_propose_split_never_shows_the_card_number(...):
    # neither the result nor a StatementError result contains "5555" or "00000000001"
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_server_split.py -v`
Expected: FAIL, the tool `propose_split` is unknown.

- [ ] **Step 3: Implement `statement_file` and `propose_split`**

Turn `StatementError` into `ToolError(str(exc))` (the messages are ours, never quoting a row). The no-match message: `f"No unbooked transaction of {total} cents between {start} and {end}; nothing is booked."`

- [ ] **Step 4: Run all tests to see them pass**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

`feat: propose a split of a card collection from its statement`.

---

### Task 5: `book_split`, and the README

**Files:**
- Modify: `src/hamurai/server.py`, `tests/test_server_split.py`, `README.md`

**Interfaces:**
- Consumes: `State.split` (Task 4), `update_booking` (Task 2), `Suggestions.learn` (Task 3).
- Produces:
  - `class SplitLine(BaseModel)`: `line: int`, `category_id: int`, `vat_code_id: int`.
  - `class SplitBooking(BaseModel)`: `line: int`, `booking_id: int`.
  - `class SplitResult(BaseModel)`: `status: Literal["booked", "rolled_back", "incomplete"]`, `bookings: list[SplitBooking]`, `left_behind: list[int]` (booking IDs that could not be deleted), `session_expired: bool`, `message: str | None`.
  - The tool `book_split(transaction_id: int, lines: list[SplitLine]) -> SplitResult`.

Before sending anything, it refuses with a `ToolError` when there is no proposed split, or when `transaction_id` is not the proposed collection (both say to run `propose_split`). It also refuses when a line number is listed twice or not at all (naming the numbers), and when a category or VAT code ID is unknown. Next it loads the suggestions, then fetches the collection's date again, and refuses when the collection is gone or `number_of_reconciliations > 0`.

Then for each line by number: `create_booking`, then `update_booking` with the line's amount, the category and VAT code, `line.description` and the collection's `entry_date`. On the first exception of any kind, it stops and deletes, in reverse order, every booking created in this call. The exceptions are `SessionExpired`, `HamuraiError`, `BookingOutcomeUnknown` and anything unexpected.

- **`rolled_back`**: every delete succeeded.
- **`incomplete`**: a delete failed, or a create's outcome is unknown. `left_behind` lists the IDs it could not delete. The message says to remove them with `undo_booking` or in the web app. For an unknown create outcome, the message also says to check the collection in Hamurai, where `propose_bookings` lists it as skipped if something was left.
- **`booked`**: everything went through. It learns every line with `learn(iban=None, name=line.description, booking=..., on=line.transaction_date)` and saves once. A failed save adds `"booked, but not learned"` to the message. `State.split` is cleared.

A session that expires sets `session_expired` and `message=SESSION_EXPIRED`, plus the left-behind note. The description says to call it only after the user explicitly approved every line.

- [ ] **Step 1: Write the failing tests**

```python
async def test_book_split_books_every_line_and_learns(...):
    # after propose_split: book_split(50, [line 1 → 10/20, line 2 → 11/21])
    assert data["status"] == "booked" and len(data["bookings"]) == 2
    # the fake's bookings on 50: amounts [-1299, -8462], descriptions the merchants', entryDate 2026-09-21
    # transaction 50 is fully reconciled; both merchants learned with their transaction dates

async def test_book_split_refuses_a_missing_or_repeated_line(...):
    # lines [1, 1] and [1] → is_error naming the line numbers; no POST sent

async def test_book_split_refuses_without_a_proposal(...): ...
async def test_book_split_refuses_unknown_ids(...): ...
async def test_book_split_refuses_a_collection_booked_since(...):
    # fake.add_booking(50, ...) between propose and book → is_error; no POST sent

async def test_failed_update_rolls_back(...):
    # fake.fail_updates = [500]: the first create succeeds, its update fails
    # → status "rolled_back"; the fake has no bookings on 50; nothing learned

async def test_failure_on_a_later_line_rolls_back_the_earlier_ones(...):
    # fake.fail_creates = [None, 500]: line 1 is booked, line 2's create fails
    # → line 1's booking deleted, "rolled_back"

async def test_undeletable_booking_is_left_behind(...):
    # fake.fail_updates = [500], fake.fail_deletes = [500] → "incomplete", left_behind == [that booking ID]

async def test_unknown_create_outcome_is_incomplete(...):
    # fake.fail_creates = [None, httpx.ReadTimeout("timeout")] → "incomplete", the message says to check the collection

async def test_session_expiry_mid_split(...):
    # expire_after_posts=1 → session_expired, message starts with SESSION_EXPIRED, the booking left behind

async def test_book_split_twice_is_refused(...):
    # a successful book_split, then the same call again → is_error; no new POST sent
```


- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_server_split.py -v`
Expected: FAIL, the tool `book_split` is unknown.

- [ ] **Step 3: Implement `book_split`**

- [ ] **Step 4: Add the README how-to**

Add a row for each new tool to "The tools". Add a "Split a credit card collection" section under "How to", after "Book a quarter". It covers downloading the statement, dragging it into the prompt, what is left out (the payment row and refunded pairs), the VAT rule for a new merchant, the all-or-nothing behaviour, and undoing a split one booking at a time.

- [ ] **Step 5: Run all checks**

Run: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run ty check`
Expected: all pass.

- [ ] **Step 6: Commit**

`feat: book a split all or nothing`.

---

## After building: the first real run

On the two Q3 collections still open (−€1.355,42 on 2026-07-20, −€293,50 on 2026-08-17): the user supplies each statement, approves the lines, and checks the split in the Hamurai web app against how they would have split it by hand. The test walkthrough goes in the PR description, not the README.
