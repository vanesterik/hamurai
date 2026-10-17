"""The MCP tools. They connect the other units and hold no logic of their own."""

import logging
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from hamurai.client import HamuraiClient, HamuraiError, SessionExpired
from hamurai.config import ConfigError, load_config
from hamurai.matcher import Labels, ProposalSet, propose_all
from hamurai.models import Category, Transaction, VatCode
from hamurai.seeding import DraftEntry, Resolution, UnresolvedError, build_draft, resolve, seedable
from hamurai.suggestions import SUGGESTIONS_PATH, Entry, SuggestionsFileError, load, save

SESSION_EXPIRED = (
    "The Hamurai session has expired. Copy a fresh Cookie header into HAMURAI_COOKIE "
    "in .env and reconnect the server with /mcp."
)

READ_ONLY = ToolAnnotations(read_only_hint=True)


class ProposeResult(BaseModel):
    proposals: ProposalSet
    hint: str | None


class PairCount(BaseModel):
    category_id: int
    vat_code_id: int
    label: str
    count: int


class FlaggedCounterparty(BaseModel):
    key: str
    name: str | None
    pairs: list[PairCount]


class SeedSummary(BaseModel):
    entries: int
    bookings: int
    skipped_split: int
    flagged: list[FlaggedCounterparty]


class SaveSummary(BaseModel):
    entries: int
    path: str


@dataclass
class State:
    client: HamuraiClient
    suggestions_path: Path
    proposed: dict[int, Transaction] = field(default_factory=dict)
    draft: list[DraftEntry] | None = None
    categories: list[Category] | None = None
    vat_codes: list[VatCode] | None = None

    async def labels(self) -> Labels:
        if self.categories is None:
            self.categories = await self.client.categories()
        if self.vat_codes is None:
            self.vat_codes = await self.client.vat_codes()
        return Labels(
            categories={c.id: f"{c.code} {c.name}" for c in self.categories},
            vat_codes={v.id: v.description for v in self.vat_codes},
        )


@contextmanager
def tool_errors() -> Generator[None]:
    """Turn the expected failures into messages the model can read. Never quotes a request."""
    try:
        yield
    except SessionExpired:
        raise ToolError(SESSION_EXPIRED) from None
    except HamuraiError as exc:
        raise ToolError(str(exc)) from None
    except SuggestionsFileError as exc:
        raise ToolError(str(exc)) from None
    except httpx.TransportError:
        raise ToolError("Could not reach Hamurai. Check the connection and try again.") from None


def build_server(client: HamuraiClient, suggestions_path: Path = SUGGESTIONS_PATH) -> MCPServer:
    state = State(client=client, suggestions_path=suggestions_path)
    mcp = MCPServer("hamurai")

    @mcp.tool(annotations=READ_ONLY)
    async def list_categories() -> list[Category]:
        """Hamurai's categories (general ledger accounts): ID, code and name."""
        with tool_errors():
            state.categories = await client.categories()
            return state.categories

    @mcp.tool(annotations=READ_ONLY)
    async def list_vat_codes() -> list[VatCode]:
        """Hamurai's VAT codes: ID, description and percentage."""
        with tool_errors():
            state.vat_codes = await client.vat_codes()
            return state.vat_codes

    @mcp.tool(annotations=READ_ONLY)
    async def propose_bookings(start_date: date, end_date: date) -> ProposeResult:
        """Propose a category and VAT code for each unbooked transaction in the period.

        Never writes anything. Proposals come in groups: confident, uncertain, unknown, and
        skipped (partly booked, never touched). Amounts are in cents.
        """
        if end_date < start_date:
            raise ToolError("end_date is before start_date")
        with tool_errors():
            suggestions = load(state.suggestions_path)
            transactions = await client.transactions(start_date, end_date)
            proposals = propose_all(transactions, suggestions, await state.labels())
        state.proposed = {t.id: t for t in transactions if not t.is_fully_reconciled}
        hint = None
        if not suggestions.entries and not suggestions.patterns:
            hint = "There are no suggestions yet: run seed_suggestions first."
        return ProposeResult(proposals=proposals, hint=hint)

    @mcp.tool(annotations=READ_ONLY)
    async def seed_suggestions(ctx: Context) -> SeedSummary:
        """Build the suggestions from the full booking history, without writing them.

        Reads every booked transaction, one request each, which takes a few minutes. Returns a
        summary with the counterparties booked inconsistently; save_suggestions writes the
        result once the user has chosen a category and VAT code for each flagged one.
        """
        with tool_errors():
            transactions = await client.transactions()
            to_read = seedable(transactions)
            reported = 0

            async def progress(done: int, total: int) -> None:
                # Requests finish out of order; progress may only go up.
                nonlocal reported
                if done > reported:
                    reported = done
                    await ctx.report_progress(done, total)

            bookings = await client.bookings_for_many([t.id for t in to_read], on_progress=progress)
            labels = await state.labels()
        history = [(t, b) for t in to_read for b in bookings[t.id]]
        state.draft = build_draft(history)
        return SeedSummary(
            entries=len(state.draft),
            bookings=len(history),
            skipped_split=sum(
                t.is_fully_reconciled and t.number_of_reconciliations > 1 for t in transactions
            ),
            flagged=[
                FlaggedCounterparty(
                    key=d.key,
                    name=d.name,
                    pairs=[
                        PairCount(
                            category_id=pair.category_id,
                            vat_code_id=pair.vat_code_id,
                            label=labels.pair(pair),
                            count=count,
                        )
                        for pair, count in sorted(
                            Entry(bookings=d.bookings).counts.items(), key=lambda item: -item[1]
                        )
                    ],
                )
                for d in state.draft
                if d.flagged
            ],
        )

    @mcp.tool()
    async def save_suggestions(resolutions: list[Resolution]) -> SaveSummary:
        """Write the seeded suggestions, with the user's choice for each flagged counterparty.

        Refuses when the suggestions file already exists: it is seeded only once.
        """
        if state.draft is None:
            raise ToolError("Nothing to save: run seed_suggestions first.")
        if state.suggestions_path.exists():
            raise ToolError(f"{state.suggestions_path} already exists; it is never overwritten.")
        try:
            suggestions = resolve(state.draft, resolutions)
        except UnresolvedError as exc:
            raise ToolError(f"Choose a category and VAT code for: {', '.join(exc.keys)}") from None
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        save(suggestions, state.suggestions_path)
        return SaveSummary(entries=len(suggestions.entries), path=str(state.suggestions_path))

    return mcp


def main() -> None:
    try:
        config = load_config()
        load(SUGGESTIONS_PATH)
    except (ConfigError, SuggestionsFileError) as exc:
        print(f"hamurai: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    # httpx logs every request URL at INFO; Hamurai's URLs carry the administration ID.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_server(HamuraiClient(config)).run()
