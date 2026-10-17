# Bank-account bookings implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, in this session, on the `bank-bookings` branch. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An MCP server that proposes a category and VAT code for each unbooked Hamurai bank transaction, books only what the user approves, and learns from each booking.

**Architecture:** Six units under `src/hamurai/`: `config`, `models`, `client` (the only network code), `suggestions` plus `seeding` (the only file code, and pure seeding logic), `matcher` (pure) and `server` (MCP tools, no logic of its own). Everything async on `httpx.AsyncClient`. Tests run against an in-memory fake of the Hamurai API mounted through `httpx.MockTransport`.

**Tech Stack:** Python 3.14, `uv`, `mcp` ≥ 2.3 (`MCPServer`), `httpx`, `pydantic` 2, `pydantic-settings`, `pyyaml`; `pytest` with `anyio`, `ruff`, `ty`.

**Spec:** `docs/specs/2026-10-17-hamurai-booking-mcp-design.md`

## Global Constraints

- Configuration only from `HAMURAI_COOKIE`, `HAMURAI_ADMINISTRATION_ID`, `HAMURAI_JOURNAL_ID`, read from the environment or from `.env` in the repository root. `HAMURAI_COOKIE` is the whole `Cookie` header (`name=value`).
- The cookie never appears in logs, errors or tool output. Never build a message from `str(exc)` of a pydantic or httpx exception, nor from a request.
- Only `client` touches the network, and only `suggestions` touches `data/suggestions.yaml`.
- Every booking is `POST`ed as `{"type": "GENERAL_LEDGER_ACCOUNT", "transactionId", "generalLedgerAccountId", "vatCodeId", "suggestion": null, "suggestionAccepted": false}`, always with a `vatCodeId`. Hamurai's suggestions endpoint is never called.
- `amount` and `amountInCents` are integers in cents.
- `.env` and `data/` are in `.gitignore`. No real administration ID, journal ID, IBAN, name or transaction in the repository, fixtures included: test data uses invented values (IBANs like `NL00TEST0000000001`).
- Before each commit: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run ty check` all pass. There is no CI.
- Commit messages: Conventional Commits, matching `chore: add gitignore file` on `main` (`feat:`, `docs:`, `chore:`), ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Push after each commit.

## Review Focus

1. **IBANs written differently** (`nl00 test 0000 0000 01` vs `NL00TEST0000000001`) must match the same counterparty. Normalised in Task 3, test `test_normalize_iban`.
2. **An empty `HAMURAI_COOKIE=` line**, as in `.env.example`, must count as missing, not as an empty cookie. Task 1, test `test_empty_value_counts_as_missing`.
3. **`save_suggestions` when `data/suggestions.yaml` already exists** must refuse rather than overwrite what was learned. Task 6, test `test_save_suggestions_refuses_to_overwrite`.
4. **The same transaction twice in one `book` call** must be refused before anything is sent. Task 7, test `test_book_refuses_duplicate_lines`.
5. **An empty string as IBAN or name** (`""`, not `null`) counts as missing: not learned under an empty key, not matched. Task 3, test `test_empty_strings_are_not_keys`.

---

### Task 1: Project setup and `config`

**Files:**
- Create: `pyproject.toml`, `src/hamurai/__init__.py`, `src/hamurai/config.py`, `tests/conftest.py`, `tests/test_config.py`
- Modify: `.gitignore` (already ignores `.env`; add `data/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`), `.env.example` (add one comment line above `HAMURAI_COOKIE`: `# The whole Cookie request header (name=value), copied from the browser.`)

**Interfaces:**
- Produces:
  - `ROOT: Path`, the repository root (`Path(__file__).resolve().parents[2]`).
  - `class ConfigError(Exception)`.
  - `class Config(BaseSettings)` with `cookie: SecretStr`, `administration_id: int`, `journal_id: int`; `env_prefix="HAMURAI_"`, `env_ignore_empty=True`, `extra="ignore"`.
  - `load_config(env_file: Path | None = ROOT / ".env") -> Config`. Raises `ConfigError` whose message lists each missing or invalid variable by name (`HAMURAI_` + field name, upper case), built only from `ValidationError.errors()` `loc` and `type`.

- [ ] **Step 1: Write `pyproject.toml`, extend `.gitignore`, write `tests/conftest.py`**

`pyproject.toml`: package `hamurai`, `src` layout, `requires-python = ">=3.14"`, script `hamurai = "hamurai.server:main"`, dependencies as in the tech stack, dev group `pytest`, `ruff`, `ty`. Ruff: `select = ["E", "F", "I", "UP", "B", "ANN"]`, `ANN` ignored under `tests/`. `conftest.py` defines the `anyio_backend` fixture returning `"asyncio"`. Run `uv sync`.

- [ ] **Step 2: Write the failing tests in `tests/test_config.py`**

```python
def test_reads_env_file(tmp_path, monkeypatch): ...
    # .env with cookie "session=abc", ids 11111 and 22222 → Config with those values;
    # config.cookie.get_secret_value() == "session=abc"
def test_environment_overrides_env_file(tmp_path, monkeypatch): ...
def test_missing_variable_is_named(tmp_path, monkeypatch):
    # .env without HAMURAI_JOURNAL_ID
    with pytest.raises(ConfigError, match="HAMURAI_JOURNAL_ID"): load_config(env)
def test_empty_value_counts_as_missing(tmp_path, monkeypatch):
    # "HAMURAI_COOKIE=" → ConfigError naming HAMURAI_COOKIE
def test_error_never_contains_the_cookie(tmp_path, monkeypatch):
    # cookie "session=SECRET-VALUE", journal id "not-a-number"
    with pytest.raises(ConfigError) as err: load_config(env)
    assert "SECRET-VALUE" not in str(err.value)
    assert "HAMURAI_JOURNAL_ID" in str(err.value)
def test_repr_hides_the_cookie(tmp_path, monkeypatch):
    assert "abc" not in repr(load_config(env))
```

Each test clears the three `HAMURAI_*` variables with `monkeypatch.delenv(..., raising=False)` first.

- [ ] **Step 3: Run `uv run pytest tests/test_config.py -v`.** Expected: FAIL, `hamurai.config` not found.

- [ ] **Step 4: Implement `src/hamurai/config.py`** per the interfaces. `load_config` passes `_env_file=env_file` to `Config`.

- [ ] **Step 5: Run the four checks from Global Constraints.** Expected: all pass.

- [ ] **Step 6: Commit and push**

```bash
git add pyproject.toml uv.lock .gitignore .env.example src/hamurai/__init__.py src/hamurai/config.py tests/conftest.py tests/test_config.py
git commit  # "feat: set up the project and read configuration from the environment"
git push
```

Check `git status` first: `.env` must not be listed.

---

### Task 2: `models` and `client`

**Files:**
- Create: `src/hamurai/models.py`, `src/hamurai/client.py`, `tests/fake_api.py`, `tests/test_client.py`

**Interfaces:**
- Consumes: `Config` (Task 1).
- Produces, in `models.py` (all frozen, `alias_generator=to_camel`, `populate_by_name=True`, `extra="ignore"`):
  - `CounterAccount`: `number: str | None = None`, `name_account_holder: str | None = None`.
  - `Transaction`: `id: int`, `entry_date: date`, `amount_cents: int` (alias `amount`), `description: str = ""`, `counter_account: CounterAccount = CounterAccount()`, `is_fully_reconciled: bool`, `number_of_reconciliations: int`.
  - `Category`: `id: int`, `code: str`, `name: str`. `VatCode`: `id: int`, `description: str`, `percentage: float`.
  - `Booking`: `id: int`, `transaction_id: int` (`AliasPath("transactionInfo", "id")`), `category_id: int` (`AliasPath("generalLedgerAccountInfo", "id")`), `vat_code_id: int` (`AliasPath("vatCode", "id")`), `amount_cents: int`.
- Produces, in `client.py`:
  - `class SessionExpired(Exception)`, `class BookingOutcomeUnknown(Exception)`, `class HamuraiError(Exception)` with `status: int` and `message: str` (Hamurai's `error.message`, or `"HTTP <status>"`).
  - `PAGE_SIZE = 500`, `BASE_URL = "https://app.hamurai.example"`.
  - `class HamuraiClient`, constructed as `HamuraiClient(config: Config, transport: httpx.AsyncBaseTransport | None = None)`, with:
    - `async transactions(start: date | None = None, end: date | None = None) -> list[Transaction]`
    - `async bookings_for(transaction_id: int) -> list[Booking]` (`GET reconciliations?transactionId=`)
    - `async bookings_for_many(transaction_ids: Sequence[int], concurrency: int = 4, on_progress: Callable[[int, int], Awaitable[None]] | None = None) -> dict[int, list[Booking]]`
    - `async categories() -> list[Category]`, `async vat_codes() -> list[VatCode]`
    - `async create_booking(transaction_id: int, category_id: int, vat_code_id: int) -> Booking`
    - `async delete_booking(booking_id: int) -> None`
    - `async aclose() -> None`
- Produces, in `tests/fake_api.py`: `class FakeHamurai`, an in-memory API. Its attributes `transactions: list[dict]`, `bookings: dict[int, dict]`, `categories: list[dict]`, `vat_codes: list[dict]`, `requests: list[httpx.Request]`, plus `expired: bool` (answer `401` with error code `2003`) and `fail_next: list[Exception | int]` (raise or answer that status once). It serves the six endpoints with the response shapes in the spec's API section, keeps `isFullyReconciled` and `numberOfReconciliations` in step with `bookings`, and exposes `transport() -> httpx.MockTransport`. Also `make_transaction(id, *, iban, name, description="", entry_date="2026-07-01", amount=-1000) -> dict` with invented data.

- [ ] **Step 1: Write `tests/fake_api.py` and the failing tests in `tests/test_client.py`**

```python
async def test_transactions_pages_until_last():
    # 1,200 fake transactions → 3 requests with page=0,1,2 and size=500; 1,200 returned
async def test_transactions_passes_journal_and_dates(): ...
    # query has journalId, startDate=2026-07-01, endDate=2026-09-30
async def test_amount_is_cents():
    assert (await client.transactions())[0].amount_cents == -1000
async def test_unauthorised_raises_session_expired():  # fake.expired = True
async def test_create_booking_sends_exact_body():
    assert json.loads(fake.requests[-1].content) == {
        "type": "GENERAL_LEDGER_ACCOUNT", "transactionId": 1, "generalLedgerAccountId": 10,
        "vatCodeId": 20, "suggestion": None, "suggestionAccepted": False}
async def test_delete_booking_uses_booking_id():
    # path ends in /reconciliations/<booking id>
async def test_reads_are_retried_once():
    # fail_next = [httpx.ConnectError("x")] → transactions() succeeds, 2 requests
async def test_reads_fail_after_second_network_error(): ...  # raises httpx.TransportError
async def test_create_booking_is_never_retried():
    # fail_next = [httpx.ReadTimeout("x")] → BookingOutcomeUnknown, exactly 1 POST
async def test_error_message_comes_from_hamurai():
    # fail_next = [422] → HamuraiError(status=422, message=<fake's error.message>)
async def test_bookings_for_many_limits_concurrency():
    # 20 ids, concurrency=4 → fake never sees more than 4 requests in flight; 20 progress calls
async def test_cookie_header_is_sent_verbatim(): ...
```

- [ ] **Step 2: Run `uv run pytest tests/test_client.py -v`.** Expected: FAIL, `hamurai.client` not found.

- [ ] **Step 3: Implement `models.py` and `client.py`.** One `httpx.AsyncClient` with `base_url`, the `Cookie` header from `config.cookie.get_secret_value()`, `Accept: application/json` and a 30-second timeout. A private `_get` retries once on `httpx.TransportError`. `create_booking` turns `httpx.TimeoutException` into `BookingOutcomeUnknown`. Every `401` becomes `SessionExpired`. Other non-2xx statuses become `HamuraiError`. `bookings_for_many` uses an `anyio.Semaphore(concurrency)` in a task group. None of these exceptions may carry the request or its headers.

- [ ] **Step 4: Run the four checks.** Expected: all pass.

- [ ] **Step 5: Commit and push.** Subject: "feat: add the Hamurai client and its models".

---

### Task 3: `suggestions` store

**Files:**
- Create: `src/hamurai/suggestions.py`, `tests/test_suggestions.py`

**Interfaces:**
- Consumes: `Transaction`, `Booking` (Task 2), `ROOT` (Task 1).
- Produces:
  - `SUGGESTIONS_PATH = ROOT / "data" / "suggestions.yaml"`.
  - `class SuggestionsFileError(Exception)`, with a message containing `line <n>` for YAML errors.
  - `normalize_iban(value: str | None) -> str | None` (upper case, whitespace removed, `""` → `None`). `normalize_name(value: str | None) -> str | None` (case-folded, inner whitespace collapsed, stripped, `""` → `None`).
  - `class Pair(BaseModel, frozen=True)`: `category_id: int`, `vat_code_id: int`.
  - `class LearnedBooking(BaseModel)`: `id: int`, `category_id: int`, `vat_code_id: int`, `date: date`.
  - `class Entry(BaseModel)`: `iban: str | None`, `name: str | None`, `bookings: list[LearnedBooking]` in the order they were learned. Properties: `current -> Pair` (the last booking's pair: the latest choice wins), `since -> date` (date of the first booking in the trailing run of `current`), `last_seen -> date`, `counts -> dict[Pair, int]`.
  - `class Pattern(BaseModel)`: `contains: str`, `category_id: int`, `vat_code_id: int`. Hand-written only.
  - `class Suggestions(BaseModel)`: `entries: list[Entry] = []`, `patterns: list[Pattern] = []`. Methods: `by_iban(iban: str | None) -> Entry | None`, `by_name(name: str | None) -> Entry | None` (both compare normalised values), `record(transaction: Transaction, booking: Booking) -> bool` (appends to the entry found by IBAN, then by name, or creates one; refreshes the entry's `name`; `False` when the transaction has neither), `forget(booking_id: int) -> bool` (removes that booking; drops an entry left with none).
  - `load(path: Path = SUGGESTIONS_PATH) -> Suggestions` (a missing file gives empty `Suggestions`), `save(suggestions: Suggestions, path: Path = SUGGESTIONS_PATH) -> None` (creates `data/`, writes a temporary file in the same directory, then `os.replace`).

The YAML layout is the builder's choice, as long as a person can read it: one line per learned booking (a flow mapping) works well.

- [ ] **Step 1: Write the failing tests in `tests/test_suggestions.py`**

```python
def test_normalize_iban():
    assert normalize_iban(" nl00 test 0000 0000 01 ") == "NL00TEST0000000001"
def test_empty_strings_are_not_keys():
    assert normalize_iban("") is None and normalize_name("  ") is None
    assert suggestions.record(tx(iban="", name=""), booking) is False
def test_record_appends_to_existing_entry_by_iban(): ...  # count for the pair goes 1 → 2
def test_record_falls_back_to_name(): ...                 # no IBAN, same name → same entry
def test_latest_choice_wins():
    # bookings 4500 ×3 then 4600 ×1 → current == Pair(4600, …), since == date of the 4600 booking
def test_forget_takes_back_count_and_restores_current():
    # forget the 4600 booking → current back to 4500, counts {4500: 3}
def test_forget_unknown_booking_returns_false(): ...
def test_missing_file_gives_empty_suggestions(tmp_path): ...
def test_save_then_load_round_trips(tmp_path): ...
def test_corrupt_file_reports_line_and_is_left_alone(tmp_path):
    # write "entries:\n  - iban: [unclosed\n"
    with pytest.raises(SuggestionsFileError, match="line"): load(path)
    assert path.read_text() == original
def test_save_is_atomic(tmp_path, monkeypatch):
    # make yaml.dump raise → the existing file is unchanged and no temporary file is left
```

- [ ] **Step 2: Run `uv run pytest tests/test_suggestions.py -v`.** Expected: FAIL, module not found.

- [ ] **Step 3: Implement `src/hamurai/suggestions.py`** per the interfaces. Use `yaml.safe_load` and `yaml.safe_dump`, and validate with `Suggestions.model_validate`. A pydantic validation error also becomes a `SuggestionsFileError`, naming the field but not the value.

- [ ] **Step 4: Run the four checks.** Expected: all pass.

- [ ] **Step 5: Commit and push.** Subject: "feat: learn, forget and store bookings in the suggestions file".

---

### Task 4: Seeding

**Files:**
- Create: `src/hamurai/seeding.py`, `tests/test_seeding.py`

Part of the `suggestions` unit, kept as pure functions so `server` can fetch and `suggestions` can write.

**Interfaces:**
- Consumes: `Transaction`, `Booking` (Task 2), `Suggestions`, `Entry`, `LearnedBooking`, `Pair`, `normalize_iban`, `normalize_name` (Task 3).
- Produces:
  - `seedable(transactions: Sequence[Transaction]) -> list[Transaction]`: fully booked with exactly one booking, and having an IBAN or a name.
  - `class DraftEntry(BaseModel)`: `key: str` (`"iban:<IBAN>"` or `"name:<normalised name>"`), `iban: str | None`, `name: str | None`, `bookings: list[LearnedBooking]` sorted by date, `flagged: bool` (more than one distinct `Pair`).
  - `build_draft(history: Sequence[tuple[Transaction, Booking]]) -> list[DraftEntry]`: groups by IBAN, otherwise by name. Flagged entries first, then by number of bookings, descending.
  - `class Resolution(BaseModel)`: `key: str`, `category_id: int`, `vat_code_id: int`.
  - `class UnresolvedError(Exception)`, carrying `keys: list[str]`.
  - `resolve(draft: Sequence[DraftEntry], resolutions: Sequence[Resolution]) -> Suggestions`: for a flagged entry, keeps only the bookings matching its resolution. Raises `UnresolvedError` for flagged entries without a resolution. Raises `ValueError` for a resolution whose key isn't in the draft, or whose pair isn't in that entry's history.

- [ ] **Step 1: Write the failing tests in `tests/test_seeding.py`**

```python
def test_seedable_skips_split_partly_booked_and_anonymous(): ...
def test_groups_by_iban_then_name(): ...
    # same IBAN with two spellings of the name → one entry; no IBAN, same name → one "name:" entry
def test_inconsistent_counterparty_is_flagged(): ...
def test_resolve_keeps_only_the_chosen_pair():
    # 4500 ×5, 4600 ×2, resolved to 4500 → entry counts {Pair(4500, …): 5}
def test_resolve_refuses_unresolved_flags():
    with pytest.raises(UnresolvedError) as err: resolve(draft, [])
    assert err.value.keys == ["iban:NL00TEST0000000001"]
def test_resolve_refuses_pair_outside_history(): ...
```

- [ ] **Step 2: Run `uv run pytest tests/test_seeding.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement `src/hamurai/seeding.py`** per the interfaces.

- [ ] **Step 4: Run the four checks.** Expected: all pass.

- [ ] **Step 5: Commit and push.** Subject: "feat: build the suggestions from the booking history".

---

### Task 5: `matcher`

**Files:**
- Create: `src/hamurai/matcher.py`, `tests/test_matcher.py`

**Interfaces:**
- Consumes: `Transaction` (Task 2), `Suggestions`, `Entry`, `Pair` (Task 3).
- Produces:
  - `class Confidence(StrEnum)`: `CONFIDENT = "confident"`, `UNCERTAIN = "uncertain"`, `UNKNOWN = "unknown"`.
  - `@dataclass(frozen=True) class Labels`: `categories: Mapping[int, str]` (e.g. `"4500 Kantoorkosten"`, code and name, because codes aren't unique), `vat_codes: Mapping[int, str]` (the VAT code's `description`). A missing ID shows as `"#<id>"`.
  - `class Proposal(BaseModel)`: `transaction: Transaction`, `confidence: Confidence`, `category_id: int | None`, `vat_code_id: int | None`, `evidence: str`.
  - `class ProposalSet(BaseModel)`: `confident`, `uncertain`, `unknown: list[Proposal]`, `skipped: list[Transaction]`.
  - `propose(transaction: Transaction, suggestions: Suggestions, labels: Labels) -> Proposal`: IBAN match → `confident`; else name match → `uncertain`; else the first pattern whose `contains` occurs in the description (case-insensitive) → `uncertain`; else `unknown`.
  - `propose_all(transactions: Sequence[Transaction], suggestions: Suggestions, labels: Labels) -> ProposalSet`: leaves out fully booked transactions, and puts partly booked ones (`number_of_reconciliations > 0`) in `skipped`.

Exact evidence text (tested):
- One pair: `IBAN match, 14× booked as 4500 Kantoorkosten / 21% hoog, last seen 2026-07-03`
- Changed: `IBAN match, now 4600 Reiskosten / 21% hoog (1×, since 2026-10-01); before that 14× 4500 Kantoorkosten / 21% hoog`. Further older pairs are joined with `; `, by count, descending.
- Name: the same, starting with `Name match` instead of `IBAN match`.
- Pattern: `Description contains "bankkosten"`
- None: `No match`

- [ ] **Step 1: Write the failing tests in `tests/test_matcher.py`**

```python
@pytest.mark.parametrize(("tx", "confidence", "evidence"), [...])  # one row per evidence form above
def test_propose(tx, confidence, evidence): ...
def test_iban_match_wins_over_name_of_another_entry(): ...
def test_changed_counterparty_proposes_the_latest_pair(): ...
def test_propose_all_groups_and_skips():
    # fully booked → absent; partly booked → skipped; others → their group
```

- [ ] **Step 2: Run `uv run pytest tests/test_matcher.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement `src/hamurai/matcher.py`** per the interfaces. No I/O.

- [ ] **Step 4: Run the four checks.** Expected: all pass.

- [ ] **Step 5: Commit and push.** Subject: "feat: match transactions against the suggestions".

---

### Task 6: `server`: reading, proposing and seeding

**Files:**
- Create: `src/hamurai/server.py`, `tests/test_server_read.py`, `.mcp.json`, `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `SESSION_EXPIRED = "The Hamurai session has expired. Copy a fresh Cookie header into HAMURAI_COOKIE in .env and reconnect the server with /mcp."`
  - `@dataclass class State`: `client: HamuraiClient`, `suggestions_path: Path`, `proposed: dict[int, Transaction]` (filled by `propose_bookings`, replaced on each call), `draft: list[DraftEntry] | None`, and cached `categories` and `vat_codes`.
  - `build_server(client: HamuraiClient, suggestions_path: Path = SUGGESTIONS_PATH) -> MCPServer`: registers the tools on a `State`.
  - `main() -> None`: `load_config()`, then `load(SUGGESTIONS_PATH)` as a startup check. On `ConfigError` or `SuggestionsFileError` it prints the message to stderr and exits with status 1. Otherwise it runs `build_server(HamuraiClient(config)).run()` over stdio.
  - Tools (read-only ones annotated `read_only_hint=True`):
    - `list_categories() -> list[Category]`, `list_vat_codes() -> list[VatCode]`
    - `propose_bookings(start_date: date, end_date: date) -> ProposeResult`, where `class ProposeResult(BaseModel)` has `proposals: ProposalSet` and `hint: str | None`. Raises `ToolError` when `end_date < start_date`. When the suggestions are empty, `hint` says to run `seed_suggestions` first.
    - `seed_suggestions(ctx: Context) -> SeedSummary`: fetches all transactions, takes `seedable(...)`, calls `bookings_for_many` with progress reported through `ctx.report_progress`, then `build_draft` into `State.draft`. Writes nothing. `SeedSummary` gives the number of entries and bookings, and lists every flagged entry with its key, name, and each pair with its labels and count.
    - `save_suggestions(resolutions: list[Resolution]) -> SaveSummary`: refuses with `ToolError` when there's no draft, when the file already exists, or on `UnresolvedError` (listing the keys). Otherwise it calls `save(resolve(...))` and returns the number of entries written.
  - Every tool turns `SessionExpired` into `ToolError(SESSION_EXPIRED)` and `HamuraiError` into `ToolError(f"Hamurai answered {status}: {message}")`. A helper or decorator in `server.py` does this once for all tools.
  - `.mcp.json`: the server `hamurai`, run as `uv run hamurai`.
  - `README.md`: setup (`uv sync`, fill in `.env` from `.env.example`, reconnect with `/mcp`), the seeding step, a quarter's session, and refreshing the cookie.

- [ ] **Step 1: Write the failing tests in `tests/test_server_read.py`.** Use `async with Client(build_server(client_on_fake, tmp_path / "suggestions.yaml")) as c:` and `c.call_tool(...)`.

```python
async def test_propose_bookings_groups_the_period(): ...
async def test_propose_bookings_rejects_reversed_dates(): ...      # is_error, "end_date"
async def test_propose_bookings_hints_at_seeding_when_empty(): ...
async def test_seed_then_save_writes_the_file(): ...
async def test_save_suggestions_refuses_unresolved(): ...          # is_error, lists the key
async def test_save_suggestions_refuses_to_overwrite(): ...        # existing file unchanged
async def test_session_expired_message(): ...                      # fake.expired → is_error, SESSION_EXPIRED text
async def test_cookie_never_in_output():
    # cookie "session=SECRET-VALUE"; call every tool in this task with fake.expired True and False,
    # and with fail_next=[500]; "SECRET-VALUE" in no content or structured_content
```

- [ ] **Step 2: Run `uv run pytest tests/test_server_read.py -v`.** Expected: FAIL.

- [ ] **Step 3: Implement `src/hamurai/server.py`, `.mcp.json` and `README.md`** per the interfaces. Tool descriptions say what each tool does. `propose_bookings` also says it never writes.

- [ ] **Step 4: Run the four checks.** Expected: all pass.

- [ ] **Step 5: Manual check.** Run `/mcp` in Claude Code: `hamurai` connects and lists five tools. Call `list_vat_codes` against the live API: it answers. Don't seed yet.

- [ ] **Step 6: Commit and push.** Subject: "feat: add the MCP server to propose bookings and seed the suggestions".

---

### Task 7: `server`: booking and undoing

**Files:**
- Modify: `src/hamurai/server.py`
- Create: `tests/test_server_book.py`

**Interfaces:**
- Consumes: `State`, `build_server` and the error helper (Task 6), `Suggestions.record` and `forget`, `load` and `save` (Task 3).
- Produces:
  - `class BookingLine(BaseModel)`: `transaction_id: int`, `category_id: int`, `vat_code_id: int`.
  - `class LineResult(BaseModel)`: `transaction_id: int`, `status: Literal["booked", "failed", "unknown", "not_tried"]`, `booking_id: int | None`, `reason: str | None`.
  - `class BookResult(BaseModel)`: `lines: list[LineResult]`, `session_expired: bool`, `message: str | None` (`SESSION_EXPIRED` when it applies).
  - `book(bookings: list[BookingLine]) -> BookResult`. Its description: *"Books the given lines in Hamurai. Call this only after the user has explicitly approved these exact lines."* The whole call is refused with `ToolError`, before any request, when: the list is empty; a `transaction_id` occurs twice; a transaction isn't in `State.proposed`; or a category or VAT code ID isn't in Hamurai's lists. Then it fetches the transactions between the lowest and highest `entry_date` of the lines again. Any line whose transaction now has `number_of_reconciliations > 0` gets `failed`, with reason `already booked`. The rest are posted one at a time:
    - success → `booked`; record and save the suggestions right away;
    - `HamuraiError` → `failed` with its message;
    - `BookingOutcomeUnknown` → `unknown`, with reason `no answer from Hamurai; run propose_bookings to see whether it was booked`;
    - `SessionExpired` → stop: this line and all later ones `not_tried`, `session_expired=True`.
  - `undo_booking(booking_id: int) -> UndoResult` (`booking_id: int`, `forgotten: bool`): deletes the booking. Only after that succeeds does it `forget` the booking in the suggestions and save.

- [ ] **Step 1: Write the failing tests in `tests/test_server_book.py`**

```python
async def test_book_books_and_learns(): ...                       # fake has the booking; file count +1
async def test_book_refuses_duplicate_lines(): ...                # is_error; fake.requests has no POST
async def test_book_refuses_unproposed_transaction(): ...         # no POST
async def test_book_refuses_unknown_category_or_vat(): ...        # no POST
async def test_book_skips_already_booked(): ...                   # booked elsewhere after propose → failed "already booked", no POST for it
async def test_one_failing_line_does_not_stop_others(): ...       # fail_next=[422] → failed, booked
async def test_timeout_is_unknown_and_not_learned(): ...          # fail_next=[httpx.ReadTimeout] → unknown; file unchanged for it
async def test_session_expired_stops_the_batch():
    # 3 lines, expire after the first POST → booked, not_tried, not_tried; session_expired True
async def test_undo_deletes_and_takes_back_count(): ...           # current pair restored
async def test_undo_after_reconnect_uses_the_file(): ...          # new build_server on same file → forgotten True
async def test_cookie_never_in_output(): ...                      # as in Task 6, for book and undo_booking
```

- [ ] **Step 2: Run `uv run pytest tests/test_server_book.py -v`.** Expected: FAIL.

- [ ] **Step 3: Add `book` and `undo_booking` to `src/hamurai/server.py`** per the interfaces.

- [ ] **Step 4: Run the four checks.** Expected: all pass.

- [ ] **Step 5: Commit and push.** Subject: "feat: book approved lines and undo bookings".

- [ ] **Step 6: Hand over the first real run to the user** (the spec's last test step): seed, then book a handful of transactions and compare them in the Hamurai web app. The user does this; nothing is booked live without them.
