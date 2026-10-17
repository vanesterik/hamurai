# Linking receipts implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, in this session, on the `link-receipts` branch. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two MCP tools: `propose_receipts` matches a period's unlinked receipts to its expenses, and `link_receipts` links the approved pairs.

**Architecture:** `client` gains the files list and the attach request, and `models` gains `File`. A new pure `receipts` unit turns transactions, the bookings of split transactions, and receipts into a proposal. `server` connects them, keeping the latest proposal in its state, as `book` does with `propose_bookings`.

**Tech Stack:** Python 3.14, `uv`, `mcp` (`MCPServer`), `httpx`, `pydantic` 2; `pytest` with `anyio`, `ruff`, `ty`.

**Spec:** `docs/specs/2026-10-17-link-receipts-design.md`

## Global Constraints

- A file's `secret` is never modelled, kept, returned or logged. `File` ignores unknown fields, as every model does.
- No real data in fixtures: invented file names, IDs and amounts.
- Only `client` touches the network. `receipts` is pure.
- A link request is never retried. `link_receipts` writes only pairs from the latest `propose_receipts`.
- Before each commit: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run ty check` all pass.
- Commits: Conventional Commits, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Check the branch in the same command as each commit and push, and push with `link-receipts:link-receipts`.

## Review Focus

1. **A receipt with an amount but no date** (or a date but no amount) can't be matched. It must be counted, not crash the matcher. Task 2, test `test_receipt_without_date_is_counted`.
2. **An archived file** must never be proposed, even though nothing links it. Task 1, test `test_archived_file_is_not_unlinked`.
3. **A receipt linked in the web app between proposing and linking** must be refused, with no request sent. Task 4, test `test_link_receipts_refuses_a_receipt_linked_meanwhile`.
4. **The receipt's amount sign.** Hamurai's metadata is positive and expenses are negative, so a match compares the absolute amount. A positive transaction (income) is never a target. Task 2, test `test_income_is_not_a_target`.
5. **The files listing stops paging** at the first file created before the cutoff, and also on an empty page, so it can never loop forever. Task 1, test `test_files_stops_on_an_empty_page`.

---

### Task 1: Files in `models` and `client`

**Files:**
- Modify: `src/hamurai/models.py`, `src/hamurai/client.py`, `tests/fake_api.py`, `tests/test_client.py`

**Interfaces:**
- Produces:
  - `class File(_Model)`: `id: int`, `created_at: datetime`, `name: str`, `mime_type: str`, `amount_cents: int | None` and `receipt_date: date | None` (from `metadata.amountInCents` and `metadata.date`, either may be missing or null), `archived_at: datetime | None`, and the link IDs `transaction_id`, `invoice_id`, `order_id`, `journal_record_id`, `estimate_id`, `reminder_id`, `billable_item_sheet_id`, `bank_transaction_import_id` (all `int | None`). The property `is_linked` is `True` when any link ID is set or `archived_at` is set.
  - `Transaction.has_files: bool = False` (from `hasFiles`).
  - `Booking.description: str = ""` (null → `""`).
  - `HamuraiClient.files(since: date) -> list[File]`: `GET /files`, then `offset=100`, `offset=200`, and so on. It stops after the first page holding a file created before `since`, or on an empty page. Read requests are retried once, as `_get` does.
  - `HamuraiClient.attach_file(file_id: int, transaction_id: int) -> File`: `POST /files/{file_id}/attach` with exactly `{"transactionId": transaction_id}`. Error handling as in `create_booking`: `ConnectError` → `HamuraiError(0, ...)`, other `TransportError` or an unreadable answer → `BookingOutcomeUnknown`, never retried.
  - In the fake: `files: list[dict]` served newest first by `createdAt`, 100 per page by `offset`, ignoring other parameters. `make_file(id, *, created_at, amount_cents=None, receipt_date=None, transaction_id=None, archived_at=None)` builds one with a `secret` field. Attach sets `transactionId`, sets `hasFiles` on the transaction, and answers `200` with the file. `fail_attaches: list[Failure]` behaves like `fail_creates`.

- [ ] **Step 1: Write the failing tests in `tests/test_client.py`**

```python
async def test_files_reads_receipts(client, fake):
    # one file with metadata {amountInCents: 875, date: "2026-07-01"} and a secret
    [file] = await client.files(date(2026, 1, 1))
    assert (file.amount_cents, file.receipt_date, file.is_linked) == (875, date(2026, 7, 1), False)
    assert "secret" not in file.model_dump_json()

async def test_files_pages_by_offset_until_the_date(client, fake):
    # 250 files created on consecutive days back from 2026-10-01
    # since = the 150th file's date → offsets [0, 100] requested, not 200

async def test_files_stops_on_an_empty_page(client, fake): ...
async def test_archived_file_is_not_unlinked(client, fake): ...
async def test_file_without_metadata(client, fake):
    # metadata null, and metadata {} → amount_cents and receipt_date None

async def test_attach_file_sends_the_transaction(client, fake):
    assert json.loads(fake.requests[-1].content) == {"transactionId": 1}
    # the answer's transaction_id == 1 and is_linked

async def test_attach_file_is_never_retried(client, fake):
    # fail_attaches = [httpx.ReadTimeout("timeout")] → BookingOutcomeUnknown, one request

async def test_transaction_has_files_and_booking_description(client, fake): ...
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_client.py -v`
Expected: FAIL, `AttributeError: 'HamuraiClient' object has no attribute 'files'`

- [ ] **Step 3: Implement the fake's files, the models and the two client calls**

- [ ] **Step 4: Run all checks**

Run: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run ty check`
Expected: all pass.

- [ ] **Step 5: Commit**

`feat: read receipts and attach one to a transaction`.

---

### Task 2: The `receipts` unit

**Files:**
- Create: `src/hamurai/receipts.py`, `tests/test_receipts.py`

**Interfaces:**
- Consumes: `File`, `Transaction`, `Booking` (Task 1).
- Produces:
  - `class Target(BaseModel, frozen=True)`: `transaction_id: int`, `date: date`, `amount_cents: int` (negative), `counterparty: str` (the counterparty's name, or the booking's description for a split line, or the transaction's description when there is no name), `split_line: bool`.
  - `class Receipt(BaseModel, frozen=True)`: `file_id: int`, `name: str`, `date: date`, `amount_cents: int` (positive).
  - `class Pair(BaseModel)`: `receipt: Receipt`, `target: Target`.
  - `class Ambiguous(BaseModel)`: `receipts: list[Receipt]`, `targets: list[Target]`.
  - `class ReceiptProposal(BaseModel)`: `confident: list[Pair]`, `ambiguous: list[Ambiguous]`, `unmatched: list[Receipt]`, `without_amount: int`.
  - `targets(transactions: Sequence[Transaction], splits: Mapping[int, Sequence[Booking]]) -> list[Target]`. `splits` holds the bookings of every transaction with more than one booking. Positive transactions and positive bookings are not targets. A non-split transaction with `has_files` is not a target.
  - `match(targets: Sequence[Target], files: Sequence[File]) -> ReceiptProposal`. Only unlinked files are used. A file missing an amount or a date counts in `without_amount`.
  - `BANK_WINDOW = (timedelta(days=30), timedelta(days=3))` (before, after) and `SPLIT_WINDOW = timedelta(days=45)` (before, none after).

Grouping, as the spec says. Build a bipartite graph of receipts against target groups, where a group is the split lines of one transaction with the same amount (a non-split target is a group of one). Each connected component is either confident or ambiguous:

```
for each connected component:
    if it is one group with n targets and m receipts, and m <= n:
        confident: pair each receipt with one of the group's targets
    else:
        ambiguous(receipts, targets of the component)
receipts in no component → unmatched
```

- [ ] **Step 1: Write the failing tests in `tests/test_receipts.py`**

```python
def test_exact_amount_in_the_window_is_confident(): ...
    # bank transaction -875 on 2026-07-01, receipt 875 dated 2026-06-30 → one confident pair

def test_bank_window_edges():
    # receipt dated 30 days before → match; 31 days before → unmatched; 3 after → match; 4 → unmatched

def test_split_line_window_edges():
    # split line on 2026-08-17; receipt 45 days before → match; 46 → unmatched; 1 day after → unmatched

def test_two_receipts_for_one_target_are_ambiguous(): ...
def test_one_receipt_for_two_targets_is_ambiguous(): ...
def test_identical_split_lines_take_as_many_receipts():
    # two -1299 lines on one split, two 1299 receipts → two confident pairs, same transaction_id
    # three receipts → one ambiguous group

def test_income_is_not_a_target(): ...
def test_credit_line_is_not_a_target(): ...
def test_transaction_with_files_is_not_a_target():
    # but a split transaction with files still gives its lines

def test_linked_and_archived_files_are_ignored(): ...
def test_receipt_without_date_is_counted():
    # amount but no date, date but no amount, neither → without_amount == 3, nothing else

def test_split_line_shows_its_merchant():
    # booking description "ACME SOFTWARE" → target.counterparty == "ACME SOFTWARE", split_line True
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_receipts.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'hamurai.receipts'`

- [ ] **Step 3: Implement `src/hamurai/receipts.py`**

- [ ] **Step 4: Run all checks**

Expected: all pass.

- [ ] **Step 5: Commit**

`feat: match receipts to a period's expenses`.

---

### Task 3: `propose_receipts`

**Files:**
- Modify: `src/hamurai/server.py`
- Create: `tests/test_server_receipts.py`

**Interfaces:**
- Consumes: `files`, `bookings_for_many` (existing), `targets`, `match`, `ReceiptProposal` (Tasks 1–2).
- Produces:
  - The tool `propose_receipts(start_date: date, end_date: date) -> ReceiptProposal`, with `READ_ONLY`.
  - `State.receipts: ReceiptProposal | None`, set by each successful call.
  - `RECEIPT_LOOKBACK = timedelta(days=45)`: `files(since=start_date - RECEIPT_LOOKBACK)`.

It fetches the period's transactions, then the bookings of those with `number_of_reconciliations > 1` (with `bookings_for_many`), then the files. The description says: it writes nothing; amounts are in cents; show the confident pairs as one table and each ambiguous group one at a time, where the user picks one pair or none; then call `link_receipts`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_propose_receipts_matches_bank_and_split(client, fake, path):
    # a bank transaction -875 with a receipt; a split transaction (two bookings -1299, -500)
    # with a receipt 1299 → two confident pairs; writes nothing

async def test_propose_receipts_reads_files_back_45_days(...):
    # the files listing stops at the page holding a file created before start - 45 days

async def test_propose_receipts_never_shows_a_secret(...):
    # the fake's secret "SECRET-FILE" appears in no tool output
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_server_receipts.py -v`
Expected: FAIL, the tool `propose_receipts` is unknown.

- [ ] **Step 3: Implement `propose_receipts`**

- [ ] **Step 4: Run all checks**

Expected: all pass.

- [ ] **Step 5: Commit**

`feat: propose receipts for a period`.

---

### Task 4: `link_receipts`, and the README

**Files:**
- Modify: `src/hamurai/server.py`, `tests/test_server_receipts.py`, `README.md`

**Interfaces:**
- Consumes: `State.receipts` (Task 3), `attach_file`, `files` (Task 1).
- Produces:
  - `class ReceiptLink(BaseModel)`: `file_id: int`, `transaction_id: int`.
  - `class LinkResult(BaseModel)`: `file_id: int`, `transaction_id: int`, `status: Literal["linked", "failed", "unknown", "not_tried"]`, `reason: str | None = None`.
  - `class LinkReceiptsResult(BaseModel)`: `links: list[LinkResult]`, `session_expired: bool`, `message: str | None`.
  - The tool `link_receipts(links: list[ReceiptLink]) -> LinkReceiptsResult`.

Before sending anything, it refuses with a `ToolError`:

- an empty list,
- a receipt listed twice,
- no proposal,
- a pair not in the latest proposal: a confident pair, or a receipt and a transaction from the same ambiguous group,
- a receipt that, by a fresh `files(since=...)` listing, is now linked or gone.

Then it attaches each link in order. A `HamuraiError` fails that link only. `SessionExpired` stops the batch, and the remaining links are `not_tried`. Anything else is `unknown`, and the reason says to run `propose_receipts` again.

The description says to call it only after the user explicitly approved these exact pairs.

- [ ] **Step 1: Write the failing tests**

```python
async def test_link_receipts_links_the_approved_pairs(...)
    # the fake's file now has transactionId; the transaction has_files
async def test_link_receipts_refuses_an_unproposed_pair(...)
async def test_link_receipts_accepts_a_pick_from_an_ambiguous_group(...)
async def test_link_receipts_refuses_a_receipt_linked_meanwhile(...)
    # fake file gets a transactionId after proposing → is_error, no POST
async def test_link_receipts_refuses_a_receipt_twice(...)
async def test_one_failed_link_does_not_stop_the_rest(...)
async def test_session_expiry_stops_the_links(...)
async def test_unknown_link_outcome(...)
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_server_receipts.py -v`
Expected: FAIL, the tool `link_receipts` is unknown.

- [ ] **Step 3: Implement `link_receipts`**

- [ ] **Step 4: README**

Add rows for both tools to "The tools", change the tool count under "Connect" from nine to eleven, and add a "Link receipts" how-to after "Split a credit card collection". It covers:

- what is matched, and how;
- the two windows;
- confident pairs versus ambiguous groups;
- that a card split's receipts are linked to its collection;
- receipts without an amount, which are only counted;
- unlinking in the web app.

- [ ] **Step 5: Run all checks**

Expected: all pass.

- [ ] **Step 6: Commit**

`feat: link approved receipts`.

---

## After building: the first real run

`propose_receipts` for Q3 2026, and for Q2 2026 (1 April – 30 June), with the user checking any proposal in the web app before approving it. The test walkthrough goes in the PR description.
