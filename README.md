# Hamurai

An MCP server that books Hamurai bank transactions with Claude Code. It proposes a category and VAT code for each unbooked transaction, from how the same counterparty was booked before, and books only what you approve.

The design is in `docs/specs/2026-10-17-hamurai-booking-mcp-design.md`.

## Setup

1. `uv sync`
2. Copy `.env.example` to `.env` and fill it in:
   - `HAMURAI_COOKIE`: the whole `Cookie` request header (`name=value`). In the browser, open Hamurai, then DevTools → Network, pick any `/api/v2/...` request, and copy the value of its `Cookie` header.
   - `HAMURAI_ADMINISTRATION_ID` and `HAMURAI_JOURNAL_ID`: the two numbers in the URL of those requests (`/administrations/<id>/...?journalId=<id>`).
3. Start Claude Code in this directory. It picks up the `hamurai` server from `.mcp.json`; `/mcp` shows whether it is connected.

`.env` and `data/` are git-ignored: the cookie and the learned bookings never go into the repository.

## Seeding, once

Ask Claude to seed the suggestions. `seed_suggestions` reads the whole booking history (a few minutes: one request per booked transaction) and lists the counterparties that were booked under more than one category or VAT code. Choose one for each, and `save_suggestions` writes `data/suggestions.yaml`. It refuses to overwrite an existing file.

Hand-written description patterns can be added to that file for transactions without an IBAN or name, such as bank fees:

```yaml
patterns:
- {contains: bankkosten, category_id: 123, vat_code_id: 45}
```

## A quarter's session

Ask Claude to book the quarter. It calls `propose_bookings` for the period, shows the confident proposals as one table for you to approve or strike lines from, and then goes through the uncertain and unknown ones one at a time. Only `book` writes to Hamurai, and Claude Code asks your permission for each call. Every approved booking is learned. `undo_booking` deletes a booking and takes back what it taught.

## When the session expires

Hamurai sessions last about a day. When a tool says the session has expired, copy a fresh `Cookie` header into `.env` and reconnect the server with `/mcp`.

## Checks

```bash
uv run pytest && uv run ruff check && uv run ruff format --check && uv run ty check
```
