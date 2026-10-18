# Payment-provider IBANs implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, in this session, on the `provider-ibans` branch. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transactions on a hand-kept list of payment-provider IBANs are matched and learned by merchant name, and the history booked on them is relearned by merchant.

**Architecture:** `Suggestions` gains the list and the provider rule for learning and name lookups. `_match_counterparty` skips the IBAN lookup for listed IBANs. A new tool, `relearn_providers`, rebuilds the provider history from Hamurai.

**Tech Stack:** Python 3.14, `uv`, `mcp` (`MCPServer`), `pydantic` 2, `pyyaml`; `pytest` with `anyio`, `ruff`, `ty`.

**Spec:** `docs/specs/2026-10-18-payment-provider-ibans-design.md`

## Global Constraints

- No real data in fixtures: provider IBANs like `NL00PROV0000000001`, invented merchants.
- Only `suggestions` touches `data/suggestions.yaml`; `relearn_providers` writes it once, atomically, through `save`, and never writes Hamurai.
- Transactions on IBANs not on the list behave exactly as before: the existing tests pass unchanged.
- Before each commit: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run ty check` all pass.
- Commits: Conventional Commits, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Check the branch in the same command as each commit and push, and push with `provider-ibans:provider-ibans`.

## Review Focus

1. **A provider IBAN typed by hand with spaces or lower case** in `provider_ibans` must still count. Task 1, test `test_provider_ibans_are_normalised`.
2. **A provider transaction without a counterparty name** can't be learned by name; it must be booked but not learned, never learned under the provider's IBAN. Task 1, test `test_provider_transaction_without_name_is_not_learned`.
3. **Running `relearn_providers` twice** gives the same file. Task 3, test `test_relearn_providers_twice_gives_the_same_file`.
4. **A merchant's later direct booking** must stay its current choice after its older provider bookings are relearned into the same entry. Task 3, test `test_relearn_keeps_a_later_direct_booking_current`.
5. **`undo_booking` on a relearned booking** takes it back from the merchant's entry. Task 3, test `test_undo_after_relearn`.

---

### Task 1: The provider list and rule in `suggestions`

**Files:**
- Modify: `src/hamurai/suggestions.py`, `tests/test_suggestions.py`

**Interfaces:**
- Produces:
  - `Suggestions.provider_ibans: list[str] = []`, a top-level YAML key, kept by `save` (written after `patterns`).
  - `Suggestions.is_provider(iban: str | None) -> bool`: compares normalised IBANs.
  - `Suggestions.by_name(name)` skips entries whose own IBAN is a provider IBAN.
  - `Suggestions.learn(...)` treats a provider IBAN as no IBAN: the booking is learned by name alone, and is not learned (returns `False`) without a name.
  - `Suggestions.sort_bookings(entry: Entry) -> None` (or a method on `Entry`): sorts the entry's bookings by date, stable, so the latest is `current`.

- [ ] **Step 1: Write the failing tests**

```python
def test_provider_ibans_are_normalised(): ...
    # Suggestions(provider_ibans=["nl00 prov 0000 0000 01"]).is_provider("NL00PROV0000000001")

def test_provider_ibans_survive_a_save(tmp_path): ...
def test_two_merchants_on_a_provider_iban_are_two_entries():
    # record() of a "Shop A" and a "Shop B" transaction on the provider IBAN → two entries, iban None

def test_provider_transaction_without_name_is_not_learned(): ...
def test_old_provider_entry_never_answers_a_name_lookup():
    # an entry with iban = provider IBAN and name "Shop A" → by_name("Shop A") is None

def test_sort_bookings_makes_the_latest_current(): ...
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_suggestions.py -v`
Expected: FAIL, `provider_ibans` is not a field.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run all checks**

Expected: all pass, the existing tests unchanged.

- [ ] **Step 5: Commit**

`feat: keep a list of payment-provider IBANs and learn them by merchant`.

---

### Task 2: Matching on a provider IBAN

**Files:**
- Modify: `src/hamurai/matcher.py`, `tests/test_matcher.py`

**Interfaces:**
- Consumes: `is_provider`, `by_name` (Task 1).
- Changes: `_match_counterparty` skips `by_iban` when `suggestions.is_provider(account.number)`; the evidence for that name match says `"Merchant match via payment provider"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_provider_transaction_matches_its_merchant():
    # provider IBAN listed; entries: the old provider entry (iban = provider, name "Shop A", pair 11)
    # and "Shop A" by name only (pair 10) → uncertain, (10, 20), evidence starts "Merchant match via payment provider"

def test_provider_transaction_of_an_unknown_merchant_falls_to_patterns(): ...
def test_provider_merchant_marked_always_ask_stays_uncertain(): ...
def test_iban_not_on_the_list_is_unchanged(): ...
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_matcher.py -v`
Expected: FAIL, the provider transaction gets the IBAN entry's pair.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run all checks**

Expected: all pass.

- [ ] **Step 5: Commit**

`feat: match payment-provider transactions by merchant`.

---

### Task 3: `relearn_providers`, and the README

**Files:**
- Modify: `src/hamurai/server.py`, `README.md`
- Create: `tests/test_server_providers.py`

**Interfaces:**
- Consumes: Task 1, `seedable`, `bookings_for_many`.
- Produces:
  - `class MerchantSummary(BaseModel)`: `name: str`, `bookings: int`, `category_id: int`, `vat_code_id: int`, `label: str`.
  - `class RelearnSummary(BaseModel)`: `merchants: list[MerchantSummary]`, `bookings: int`, `removed_entries: int`.
  - The tool `relearn_providers() -> RelearnSummary`.

The tool works in this order:

1. Load the suggestions, and refuse when `provider_ibans` is empty.
2. Fetch all transactions. Keep the `seedable` ones on a provider IBAN, and read their bookings with `bookings_for_many`.
3. Remove the entries whose IBAN is a provider IBAN.
4. `forget` each booking ID about to be relearned.
5. Learn each booking oldest first with `record`.
6. Sort every entry that received a booking.
7. `save` once.

Any `HamuraiError` or `SessionExpired` goes through `tool_errors`, before anything is saved. The description says that it reads the history of the provider IBANs and rebuilds their entries by merchant, that it writes only the suggestions file, and that it's safe to run again.

- [ ] **Step 1: Write the failing tests**

```python
async def test_relearn_providers_splits_merchants(...):
    # history on the provider IBAN: Shop A 2× (10/20), Shop B 1× (11/20); an old mixed provider entry
    # → entries "Shop A" (2, pair 10) and "Shop B" (1, pair 11), iban None; the old entry gone
async def test_relearn_providers_latest_choice_wins(...): ...
async def test_relearn_keeps_a_later_direct_booking_current(...):
    # "Shop A" also has a direct entry with a 2026 booking (its own IBAN); provider bookings from 2025
    # → that entry's current pair is still the 2026 one
async def test_relearn_providers_twice_gives_the_same_file(...)
async def test_relearn_providers_leaves_other_entries_alone(...)
async def test_relearn_providers_refuses_without_a_list(...)
async def test_relearn_providers_writes_nothing_when_a_read_fails(...)
async def test_book_learns_a_provider_transaction_by_merchant(...)
async def test_undo_after_relearn(...)
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_server_providers.py -v`
Expected: FAIL, the tool `relearn_providers` is unknown.

- [ ] **Step 3: Implement `relearn_providers`**

- [ ] **Step 4: README**

- Add a row for the tool to "The tools".
- Change the tool count under "Connect" from eleven to twelve.
- Add a "Payment providers" how-to after "Add a description pattern". It covers:
  - what a provider IBAN is, and why it's matched by merchant;
  - the `provider_ibans:` list, and that comments are lost when the file is saved;
  - running `relearn_providers` after listing an IBAN, and after seeding a fresh setup;
  - that a match there is `uncertain`.

- [ ] **Step 5: Run all checks**

Expected: all pass.

- [ ] **Step 6: Commit**

`feat: relearn payment-provider history by merchant`.

---

## After building: the first real run

1. Add the seven IBANs to `data/suggestions.yaml`.
2. Run `relearn_providers`, and check its summary: Spotify AB should come out with its 7 bookings.
3. Ask for proposals for Q3 2026, which is fully booked, so nothing is proposed. Then ask for Q2 2026, if it has open provider transactions, to see them proposed by merchant.
