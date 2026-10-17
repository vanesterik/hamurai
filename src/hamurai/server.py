"""The MCP tools. They connect the other units and hold no logic of their own."""

import logging
import re
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import anyio
import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from hamurai.client import HamuraiClient, HamuraiError, SessionExpired
from hamurai.config import ConfigError, load_config
from hamurai.matcher import Labels, LineProposal, ProposalSet, propose_all, propose_line
from hamurai.models import Booking, Category, Transaction, VatCode
from hamurai.receipts import ReceiptProposal, match, targets
from hamurai.seeding import DraftEntry, Resolution, UnresolvedError, build_draft, resolve, seedable
from hamurai.statement import LeftOut, Statement, StatementError, read_statement
from hamurai.suggestions import SUGGESTIONS_PATH, Entry, SuggestionsFileError, load, save

SESSION_EXPIRED = (
    "The Hamurai session has expired. Paste a fresh Authorization cookie value into "
    "HAMURAI_COOKIE in .env and reconnect the server with /mcp."
)

READ_ONLY = ToolAnnotations(read_only_hint=True)
# How long after a statement's last booking date its collection may come.
COLLECTION_WINDOW = timedelta(days=45)
# A receipt is uploaded after its date, which lies at most ~45 days before its expense.
RECEIPT_LOOKBACK = timedelta(days=45)


class ProposeResult(BaseModel):
    proposals: ProposalSet
    hint: str | None


class PairCount(BaseModel):
    category_id: int
    vat_code_id: int
    label: str
    count: int
    last_seen: date


class FlaggedCounterparty(BaseModel):
    key: str
    name: str | None
    pairs: list[PairCount]


class SplitProposal(BaseModel):
    collection: Transaction
    lines: list[LineProposal]
    left_out: list[LeftOut]
    total_cents: int


class SeedSummary(BaseModel):
    entries: int
    bookings: int
    skipped_split: int
    skipped_other: int
    flagged: list[FlaggedCounterparty]


class SaveSummary(BaseModel):
    entries: int
    path: str


class BookingLine(BaseModel):
    transaction_id: int
    category_id: int
    vat_code_id: int


class LineResult(BaseModel):
    transaction_id: int
    status: Literal["booked", "failed", "unknown", "not_tried"]
    booking_id: int | None = None
    reason: str | None = None


class BookResult(BaseModel):
    lines: list[LineResult]
    session_expired: bool
    message: str | None


class SplitLine(BaseModel):
    line: int
    category_id: int
    vat_code_id: int


class SplitBooking(BaseModel):
    line: int
    booking_id: int


class SplitResult(BaseModel):
    status: Literal["booked", "rolled_back", "incomplete"]
    bookings: list[SplitBooking]
    left_behind: list[int]
    session_expired: bool
    message: str | None


class ReceiptLink(BaseModel):
    file_id: int
    transaction_id: int


class LinkResult(BaseModel):
    file_id: int
    transaction_id: int
    status: Literal["linked", "failed", "unknown", "not_tried"]
    reason: str | None = None


class LinkReceiptsResult(BaseModel):
    links: list[LinkResult]
    session_expired: bool
    message: str | None


class UndoResult(BaseModel):
    booking_id: int
    forgotten: bool
    already_gone: bool = False


@dataclass
class State:
    client: HamuraiClient
    suggestions_path: Path
    proposed: dict[int, Transaction] = field(default_factory=dict)
    draft: list[DraftEntry] | None = None
    split: tuple[Transaction, Statement] | None = None
    receipts: ReceiptProposal | None = None
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


def statement_file(text: str) -> Path:
    """A path as typed or dragged into the terminal: quoted, or with shell escapes such as
    `\\ ` and `\\(`."""
    text = text.strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    else:
        text = re.sub(r"\\(.)", r"\1", text)
    return Path(text).expanduser()


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
        skipped (partly booked, never touched). Amounts are in cents. A credit card collection
        is split with propose_split, not booked as one line.
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
    async def propose_split(statement_path: str) -> SplitProposal:
        """Propose how to split a credit card collection, from the card statement's CSV.

        Never writes anything. Finds the one unbooked bank transaction the statement's
        purchases add up to, and proposes a category and VAT code for each line. The card
        payment row and refunded purchases are left out and listed. Amounts are in cents.

        For an unknown merchant, propose the category, and choose the VAT code by the user's
        rule: "Geen / Vrijgesteld" for a merchant located outside the EU, the 21% code for one
        inside the EU. Show the confident lines as one table and the rest one at a time. Every
        line is booked, so correct a line rather than leave it out; then call book_split.
        """
        state.split = None
        try:
            statement = read_statement(statement_file(statement_path))
        except StatementError as exc:
            raise ToolError(str(exc)) from None
        start = statement.first_booking_date
        end = statement.last_booking_date + COLLECTION_WINDOW
        with tool_errors():
            suggestions = load(state.suggestions_path)
            transactions = await client.transactions(start, end)
            labels = await state.labels()
        candidates = [
            t
            for t in transactions
            if t.number_of_reconciliations == 0 and t.amount_cents == statement.total_cents
        ]
        if not candidates:
            raise ToolError(
                f"No unbooked transaction of {statement.total_cents} cents between {start} "
                f"and {end}; nothing is booked."
            )
        if len(candidates) > 1:
            listed = "; ".join(f"{t.entry_date} {t.description}" for t in candidates)
            raise ToolError(
                f"More than one unbooked transaction of {statement.total_cents} cents: "
                f"{listed}. Nothing is booked."
            )
        [collection] = candidates
        state.split = (collection, statement)
        return SplitProposal(
            collection=collection,
            lines=[propose_line(line, suggestions, labels) for line in statement.lines],
            left_out=statement.left_out,
            total_cents=statement.total_cents,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def propose_receipts(start_date: date, end_date: date) -> ReceiptProposal:
        """Propose which unlinked receipt belongs to which expense in the period.

        Never writes anything. Matches by the amount and date Hamurai read from each
        receipt, against every expense, booked or not: bank transactions without files, and
        each line of a split (a receipt for a split line is linked to the split transaction).
        Amounts are in cents; a receipt's is positive. Show the confident pairs as one table
        and each ambiguous group one at a time, where the user picks one pair or none; then
        call link_receipts.
        """
        if end_date < start_date:
            raise ToolError("end_date is before start_date")
        state.receipts = None
        with tool_errors():
            transactions = await client.transactions(start_date, end_date)
            split_ids = [t.id for t in transactions if t.number_of_reconciliations > 1]
            splits = await client.bookings_for_many(split_ids)
            files = await client.files(start_date - RECEIPT_LOOKBACK)
        state.receipts = match(targets(transactions, splits), files)
        return state.receipts

    @mcp.tool()
    async def link_receipts(links: list[ReceiptLink]) -> LinkReceiptsResult:
        """Links the given receipts to their transactions in Hamurai.

        Call this only after the user has explicitly approved these exact pairs. Every pair
        must come from the latest propose_receipts call: a confident pair, or a receipt and a
        transaction from the same ambiguous group. One failing link doesn't stop the others;
        an expired session stops the batch and says which links were not tried.
        """
        if not links:
            raise ToolError("No receipts to link.")
        ids = [link.file_id for link in links]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            raise ToolError(f"Receipts listed more than once: {repeated}")
        if state.receipts is None:
            raise ToolError("Nothing proposed: run propose_receipts first.")
        allowed = {(p.receipt.file_id, p.target.transaction_id) for p in state.receipts.confident}
        allowed |= {
            (receipt.file_id, target.transaction_id)
            for group in state.receipts.ambiguous
            for receipt in group.receipts
            for target in group.targets
        }
        unproposed = [
            (link.file_id, link.transaction_id)
            for link in links
            if (link.file_id, link.transaction_id) not in allowed
        ]
        if unproposed:
            raise ToolError(
                f"Not in the latest proposal: {unproposed}. Run propose_receipts first."
            )
        # As many receipts per transaction as it has targets: one for a bank transaction, one
        # per line (of that amount) for a split.
        # A list, not a set: two identical lines of one split are two targets.
        proposed_targets = [p.target for p in state.receipts.confident] + [
            t for group in state.receipts.ambiguous for t in group.targets
        ]
        room: dict[int, int] = {}
        for target in proposed_targets:
            room[target.transaction_id] = room.get(target.transaction_id, 0) + 1
        asked = [link.transaction_id for link in links]
        crowded = sorted({t for t in asked if asked.count(t) > room.get(t, 0)})
        if crowded:
            raise ToolError(f"More receipts than expenses for transactions {crowded}.")
        dates = {
            r.file_id: r.date
            for r in [p.receipt for p in state.receipts.confident]
            + [r for group in state.receipts.ambiguous for r in group.receipts]
        }
        with tool_errors():
            # Listed again just before linking, so a receipt is never linked twice.
            current = {
                f.id: f for f in await client.files(min(dates[i] for i in ids) - timedelta(days=1))
            }
        taken = [i for i in ids if i not in current or current[i].is_linked]
        if taken:
            raise ToolError(f"Receipts no longer unlinked: {taken}. Nothing is linked.")

        results: list[LinkResult] = []
        expired = False
        for link in links:
            if expired:
                results.append(LinkResult(**link.model_dump(), status="not_tried"))
                continue
            try:
                await client.attach_file(link.file_id, link.transaction_id)
            except SessionExpired:
                expired = True
                results.append(LinkResult(**link.model_dump(), status="not_tried"))
                continue
            except HamuraiError as exc:
                # A gateway error may come after the link was made.
                if exc.status < 500:
                    results.append(
                        LinkResult(**link.model_dump(), status="failed", reason=exc.message)
                    )
                    continue
                results.append(
                    LinkResult(
                        **link.model_dump(),
                        status="unknown",
                        reason=f"{exc.message}; run propose_receipts to see whether it was linked",
                    )
                )
                continue
            except Exception:
                results.append(
                    LinkResult(
                        **link.model_dump(),
                        status="unknown",
                        reason="no answer from Hamurai; "
                        "run propose_receipts to see whether it was linked",
                    )
                )
                continue
            results.append(LinkResult(**link.model_dump(), status="linked"))
        return LinkReceiptsResult(
            links=results,
            session_expired=expired,
            message=SESSION_EXPIRED if expired else None,
        )

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
            skipped_other=sum(not bookings[t.id] for t in to_read),
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
                            last_seen=max(b.date for b in d.bookings if b.pair == pair),
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

        A choice is either one category_id and vat_code_id from its history, or always_ask:
        true, for a counterparty with no single right category (it is then always proposed as
        uncertain, with its history as evidence).

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

    @mcp.tool()
    async def book(bookings: list[BookingLine]) -> BookResult:
        """Books the given lines in Hamurai, as manual bookings, and learns each one.

        Call this only after the user has explicitly approved these exact lines. Every
        transaction must come from the latest propose_bookings call. One failing line doesn't
        stop the others; an expired session stops the batch and says which lines were not tried.
        """
        if not bookings:
            raise ToolError("No lines to book.")
        ids = [b.transaction_id for b in bookings]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ToolError(f"Transactions listed more than once: {duplicates}")
        unproposed = [i for i in ids if i not in state.proposed]
        if unproposed:
            raise ToolError(
                f"Not in the latest proposals: {unproposed}. Run propose_bookings first."
            )
        with tool_errors():
            labels = await state.labels()
            unknown = sorted(
                {b.category_id for b in bookings if b.category_id not in labels.categories}
                | {b.vat_code_id for b in bookings if b.vat_code_id not in labels.vat_codes}
            )
            if unknown:
                raise ToolError(f"Unknown category or VAT code IDs: {unknown}")
            suggestions = load(state.suggestions_path)
            dates = [state.proposed[i].entry_date for i in ids]
            # Fetched again just before booking, so nothing gets booked twice.
            latest = {t.id: t for t in await client.transactions(min(dates), max(dates))}

        results: list[LineResult] = []
        expired = False
        for line in bookings:
            transaction = latest.get(line.transaction_id)
            if expired:
                results.append(LineResult(transaction_id=line.transaction_id, status="not_tried"))
                continue
            if transaction is None or transaction.number_of_reconciliations > 0:
                results.append(
                    LineResult(
                        transaction_id=line.transaction_id,
                        status="failed",
                        reason="already booked" if transaction else "no longer in Hamurai",
                    )
                )
                continue
            try:
                booking = await client.create_booking(
                    line.transaction_id, line.category_id, line.vat_code_id
                )
            except SessionExpired:
                expired = True
                results.append(LineResult(transaction_id=line.transaction_id, status="not_tried"))
                continue
            except HamuraiError as exc:
                results.append(
                    LineResult(
                        transaction_id=line.transaction_id, status="failed", reason=exc.message
                    )
                )
                continue
            except Exception:
                # BookingOutcomeUnknown, or anything unexpected: once a request may have been
                # sent, every outcome must reach the report.
                results.append(
                    LineResult(
                        transaction_id=line.transaction_id,
                        status="unknown",
                        reason="no answer from Hamurai; "
                        "run propose_bookings to see whether it was booked",
                    )
                )
                continue
            reason = None
            try:
                if suggestions.record(transaction, booking):
                    save(suggestions, state.suggestions_path)
            except Exception as exc:
                reason = f"booked, but not learned ({type(exc).__name__})"
            results.append(
                LineResult(
                    transaction_id=line.transaction_id,
                    status="booked",
                    booking_id=booking.id,
                    reason=reason,
                )
            )
        return BookResult(
            lines=results,
            session_expired=expired,
            message=SESSION_EXPIRED if expired else None,
        )

    @mcp.tool()
    async def book_split(transaction_id: int, lines: list[SplitLine]) -> SplitResult:
        """Splits the card collection from the latest propose_split, one booking per line.

        Call this only after the user has explicitly approved every line. Takes the
        collection's transaction ID and every line exactly once. All or nothing: when any
        request fails, every booking made in this call is deleted again. Learns each
        merchant once the whole split is booked.
        """
        if state.split is None or state.split[0].id != transaction_id:
            raise ToolError(
                f"Transaction {transaction_id} is not the split proposed last. "
                "Run propose_split first."
            )
        collection, statement = state.split
        numbers = [line.line for line in lines]
        expected = [line.number for line in statement.lines]
        repeated = sorted({n for n in numbers if numbers.count(n) > 1})
        missing = sorted(set(expected) - set(numbers))
        unknown_lines = sorted(set(numbers) - set(expected))
        if repeated or missing or unknown_lines:
            problems = [
                f"{label} line {', '.join(map(str, found))}"
                for label, found in (
                    ("repeated", repeated),
                    ("missing", missing),
                    ("no", unknown_lines),
                )
                if found
            ]
            raise ToolError(f"Book every line exactly once: {'; '.join(problems)}.")
        # Claimed before the first await, so a parallel call is refused. Given back unless
        # the split is booked, so that it can be tried again.
        state.split = None
        try:
            result = await split(collection, statement, lines)
        except BaseException:
            state.split = (collection, statement)
            raise
        if result.status != "booked":
            state.split = (collection, statement)
        return result

    async def split(
        collection: Transaction, statement: Statement, lines: list[SplitLine]
    ) -> SplitResult:
        transaction_id = collection.id
        with tool_errors():
            labels = await state.labels()
            unknown = sorted(
                {b.category_id for b in lines if b.category_id not in labels.categories}
                | {b.vat_code_id for b in lines if b.vat_code_id not in labels.vat_codes}
            )
            if unknown:
                raise ToolError(f"Unknown category or VAT code IDs: {unknown}")
            suggestions = load(state.suggestions_path)
            # Fetched again just before booking, so nothing gets booked twice.
            latest = {
                t.id: t
                for t in await client.transactions(collection.entry_date, collection.entry_date)
            }
        current = latest.get(transaction_id)
        if current is None:
            raise ToolError(f"Transaction {transaction_id} is no longer in Hamurai.")
        if current.number_of_reconciliations > 0:
            raise ToolError(f"Transaction {transaction_id} is already booked; nothing is booked.")

        by_number = {line.number: line for line in statement.lines}
        # Credits last: the last one's create then already gets exactly its own amount.
        chosen = sorted(lines, key=lambda c: (by_number[c.line].amount_cents > 0, c.line))
        made: list[SplitBooking] = []
        failure: Exception | None = None
        # Only a create without an answer can leave a booking nobody knows the ID of.
        create_unknown = False
        # A new booking gets the collection's unbooked remainder: anything else means the
        # collection changed while it was being split.
        remainder = current.amount_cents
        try:
            for choice in chosen:
                line = by_number[choice.line]
                try:
                    created = await client.create_booking(
                        transaction_id, choice.category_id, choice.vat_code_id
                    )
                except HamuraiError as exc:
                    # A gateway error may come after the booking was made.
                    failure, create_unknown = exc, exc.status >= 500
                    break
                except SessionExpired as exc:
                    failure = exc
                    break
                except Exception as exc:
                    failure, create_unknown = exc, True
                    break
                made.append(SplitBooking(line=line.number, booking_id=created.id))
                if created.amount_cents != remainder:
                    failure = CollectionChanged()
                    break
                try:
                    await client.update_booking(
                        created.id,
                        transaction_id,
                        amount_cents=line.amount_cents,
                        category_id=choice.category_id,
                        vat_code_id=choice.vat_code_id,
                        description=line.description,
                        entry_date=collection.entry_date,
                    )
                except Exception as exc:
                    failure = exc
                    break
                remainder -= line.amount_cents
        except BaseException:
            # Cancelled partway: still undo what was made, then give up.
            with anyio.move_on_after(30, shield=True):
                await _roll_back(client, transaction_id, made, None, create_unknown=True)
            raise

        if failure is not None:
            return await _roll_back(client, transaction_id, made, failure, create_unknown)

        message = None
        try:
            for choice, made_booking in zip(chosen, made, strict=True):
                line = by_number[choice.line]
                booking = Booking(
                    id=made_booking.booking_id,
                    transaction_id=transaction_id,
                    category_id=choice.category_id,
                    vat_code_id=choice.vat_code_id,
                    amount_cents=line.amount_cents,
                )
                suggestions.learn(
                    iban=None, name=line.description, booking=booking, on=line.transaction_date
                )
            save(suggestions, state.suggestions_path)
        except Exception as exc:
            message = f"booked, but not learned ({type(exc).__name__})"
        return SplitResult(
            status="booked", bookings=made, left_behind=[], session_expired=False, message=message
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
    async def undo_booking(booking_id: int) -> UndoResult:
        """Deletes a booking in Hamurai and takes back what it taught the suggestions.

        Takes the booking ID that book returned, not the transaction ID.
        """
        already_gone = False
        with tool_errors():
            try:
                await client.delete_booking(booking_id)
            except HamuraiError as exc:
                # Gone already (deleted in the web app, or an earlier undo lost its answer):
                # still take back what it taught.
                if exc.status != 404:
                    raise
                already_gone = True
            suggestions = load(state.suggestions_path)
            forgotten = suggestions.forget(booking_id)
            if already_gone and not forgotten:
                raise HamuraiError(404, f"no booking {booking_id}, in Hamurai or learned")
            if forgotten:
                save(suggestions, state.suggestions_path)
        return UndoResult(booking_id=booking_id, forgotten=forgotten, already_gone=already_gone)

    return mcp


class CollectionChanged(Exception):
    """A new booking didn't get the expected remainder: the collection was booked meanwhile."""


async def _roll_back(
    client: HamuraiClient,
    transaction_id: int,
    made: list[SplitBooking],
    failure: Exception | None,
    create_unknown: bool,
) -> SplitResult:
    """Delete every booking a stopped split made, newest first, then look at what is left."""
    expired = isinstance(failure, SessionExpired)
    not_deleted = []
    for made_booking in reversed(made):
        try:
            await client.delete_booking(made_booking.booking_id)
        except HamuraiError as exc:
            # Gone already, deleted in the web app or by a delete whose answer was lost.
            if exc.status != 404:
                not_deleted.append(made_booking.booking_id)
        except SessionExpired:
            expired = True
            not_deleted.append(made_booking.booking_id)
        except Exception:
            not_deleted.append(made_booking.booking_id)
    not_deleted.reverse()
    # The collection was unbooked when the split started, so whatever is on it now is left.
    try:
        left_behind = [b.id for b in await client.bookings_for(transaction_id)]
        checked = True
    except SessionExpired:
        expired, checked, left_behind = True, False, not_deleted
    except Exception:
        checked, left_behind = False, not_deleted

    if failure is None:
        reason = "The split was cancelled."
    elif isinstance(failure, HamuraiError):
        reason = f"The split stopped: {failure}."
    elif isinstance(failure, CollectionChanged):
        reason = "The split stopped: the collection was booked while it was being split."
    elif isinstance(failure, SessionExpired):
        reason = "The split stopped."
    else:
        reason = "The split stopped: no answer from Hamurai."
    notes = [reason]
    unsure = bool(left_behind) or (create_unknown and not checked)
    if left_behind:
        notes.append(
            f"These bookings are still on the collection: {left_behind}. Remove them with "
            "undo_booking or in the web app."
        )
    elif not unsure:
        notes.append("Every booking it made was deleted again; nothing is booked.")
    if create_unknown and not checked:
        notes.append(
            "A booking may have been made without an answer: check the collection in "
            "Hamurai, where propose_bookings lists it as skipped if something was left."
        )
    message = " ".join(notes)
    if expired:
        message = f"{SESSION_EXPIRED} {message}"
    return SplitResult(
        status="incomplete" if unsure else "rolled_back",
        bookings=[],
        left_behind=left_behind,
        session_expired=expired,
        message=message,
    )


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
