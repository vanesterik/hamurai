# Hamurai

An MCP server that books Hamurai bank transactions with Claude Code. It proposes a category and VAT code for each unbooked transaction, from how the same counterparty was booked before, and books only what you approve.

The design is in `docs/specs/2026-10-17-hamurai-booking-mcp-design.md`, the implementation plan in `docs/plans/2026-10-17-bank-bookings.md`.

## Setup

1. `uv sync`
2. Copy `.env.example` to `.env` and fill it in:
   - `HAMURAI_COOKIE`: paste the value of Hamurai's `Authorization` cookie after the `Authorization=` that is already there. In the browser, open Hamurai, then DevTools → Application → Cookies → `app.hamurai.example`, and copy the value of `Authorization`.
   - `HAMURAI_ADMINISTRATION_ID` and `HAMURAI_JOURNAL_ID`: in DevTools → Network, open any `/api/v2/...` request. Its URL holds both numbers: `/administrations/<administration id>/...?journalId=<journal id>`.
3. Start Claude Code in this directory. It picks up the `hamurai` server from `.mcp.json`; `/mcp` shows whether it is connected.

`.env` and `data/` are git-ignored: the cookie and the learned bookings never go into the repository.

## The tools

| Tool | Writes to | What it does |
|---|---|---|
| `list_categories`, `list_vat_codes` | nothing | Hamurai's categories (ID, code, name) and VAT codes (ID, description). |
| `propose_bookings(start_date, end_date)` | nothing | Proposes a category and VAT code for each unbooked transaction in the period, in four groups: `confident` (IBAN match), `uncertain` (name or description match), `unknown` (no match) and `skipped` (partly booked, never touched). Each proposal says why. |
| `seed_suggestions` | nothing | Reads the whole booking history and builds the suggestions in memory. Lists the counterparties booked under more than one category or VAT code. |
| `save_suggestions(resolutions)` | `data/suggestions.yaml` | Writes the seeded suggestions, with your choice for each flagged counterparty. Refuses if the file already exists. |
| `book(bookings)` | Hamurai and `data/suggestions.yaml` | Books the given lines and learns each one. The only tool that creates bookings. |
| `undo_booking(booking_id)` | Hamurai and `data/suggestions.yaml` | Deletes a booking and takes back what it taught. |

Amounts in tool output are in cents: `-1299` is € −12,99.

## How to

You talk to Claude in plain words; it picks the tools. The prompts below are examples, not commands.

### Check the code

```bash
uv run pytest && uv run ruff check && uv run ruff format --check && uv run ty check
```

No test talks to Hamurai: they run against an in-memory fake with invented data.

### Connect

Run `/mcp` in Claude Code. `hamurai` should show as connected with seven tools. If it failed to start, run `uv run hamurai` in a terminal: it prints the reason and exits (a missing or empty variable in `.env` is named, never shown). If it starts, it waits for Claude Code; stop it with Ctrl-C.

After changing `.env` or the code, reconnect the server from `/mcp`: it reads `.env` only when it starts.

### Seed the suggestions, once

> Seed the suggestions from the booking history.

`seed_suggestions` takes a few minutes: one request per booked transaction. Its summary gives:

- how many counterparties and bookings it found;
- `skipped_split`: transactions split over several bookings, left out;
- `skipped_other`: transactions booked onto something other than a category, such as an invoice, left out;
- `flagged`: counterparties booked under more than one category or VAT code, each with how often each one was used.

Tell Claude which category and VAT code each flagged counterparty should get:

> For the flagged ones, take the one used most each time, except the travel agency: that's 4600 Reiskosten.

`save_suggestions` then writes `data/suggestions.yaml`. Open it: one entry per counterparty, one line per learned booking. It is meant to stay readable and can be edited by hand.

Seed before booking anything. Booking first creates the file, and `save_suggestions` never overwrites it: to seed after all, delete `data/suggestions.yaml` (losing anything learned or written in it by hand) and seed again.

### Book a quarter

> Book Q3 2026.

Claude calls `propose_bookings` for 1 July to 30 September and shows the confident proposals as one table. Approve it, or name the lines to strike. Then it goes through the uncertain and unknown ones one at a time: for an unknown one it proposes a category itself, from Hamurai's list, and you approve or correct it.

Claude Code asks your permission for every `book` call. That prompt is your hard stop: check the lines in it before you allow it, and don't choose "always allow" for `book`.

`book` reports each line:

| Status | Meaning |
|---|---|
| `booked` | Booked, with its booking ID. Learned, unless the reason says otherwise. |
| `failed` | Not booked: Hamurai refused it, or it was already booked in the meantime. |
| `unknown` | No answer from Hamurai: it may or may not be booked. Ask for the proposals again; a booked one no longer appears. Never booked twice. |
| `not_tried` | The session expired partway; these lines were not sent. |

Every booking you approve is learned. When you book a counterparty differently than before, the latest choice wins: next quarter proposes the new one, and the evidence says what it was before.

### Undo a booking

> Undo the booking for the parking garage.

`undo_booking` takes a booking ID (from `book`'s report), deletes that booking in Hamurai and takes back what it taught. If the booking was already deleted in the web app, it still takes it back. It deletes whatever booking ID it gets, including ones made by hand in the web app, so check the ID in the permission prompt.

### Add a description pattern

For transactions with neither an IBAN nor a name, such as bank fees, add a pattern to `data/suggestions.yaml` by hand. A description containing the text (case doesn't matter) gets that category and VAT code, as an `uncertain` proposal:

```yaml
patterns:
- {contains: bankkosten, category_id: 123, vat_code_id: 45}
```

Ask Claude to list the categories and VAT codes to find the IDs. No reconnect is needed: the file is read on every call.

### Refresh the session

Hamurai sessions last about a day. When a tool says the session has expired, paste a fresh `Authorization` cookie value into `.env` and reconnect the server from `/mcp`. Lines a `book` call did not get to are listed as `not_tried`; ask Claude to book them again after reconnecting.
