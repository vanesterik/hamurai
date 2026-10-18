"""The credit card statement: the only unit that reads it. Pure apart from reading the file.

The card's identifying columns (account number, card number, name on the card) are read past
and never kept, so they can't reach tool output. Errors name a row or a column, never a value.
"""

import csv
import io
import re
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from hamurai.suggestions import normalize_name

# The previous collection arriving on the card account: not a purchase.
PAYMENT_PREFIX = "BETALING ONTVANGEN"
COLUMNS = ("Transactiedatum", "Boekingsdatum", "Omschrijving", "Bedrag in EUR")
_AMOUNT = re.compile(r"(-?)(\d{1,3}(?:\.\d{3})+|\d+),(\d{2})")


# Why a row is not a line: the card payment, a refunded purchase and its refund, or nothing to
# book (a waived fee).
type Reason = Literal["card payment", "refunded", "refund", "zero"]


class StatementError(Exception):
    """The statement can't be split. Nothing is booked."""


class StatementLine(BaseModel, frozen=True):
    number: int
    transaction_date: date
    booking_date: date
    description: str
    amount_cents: int


class LeftOut(BaseModel, frozen=True):
    transaction_date: date
    description: str
    amount_cents: int
    reason: Reason


class Statement(BaseModel):
    lines: list[StatementLine]
    left_out: list[LeftOut]

    @property
    def total_cents(self) -> int:
        return sum(line.amount_cents for line in self.lines)

    @property
    def first_booking_date(self) -> date:
        return min(line.booking_date for line in self.lines)

    @property
    def last_booking_date(self) -> date:
        return max(line.booking_date for line in self.lines)


class _Row(BaseModel, frozen=True):
    row: int
    transaction_date: date
    booking_date: date
    description: str
    amount_cents: int

    def left_out(self, reason: Reason) -> LeftOut:
        return LeftOut(
            transaction_date=self.transaction_date,
            description=self.description,
            amount_cents=self.amount_cents,
            reason=reason,
        )


def parse_amount(text: str) -> int:
    """A decimal-comma amount, with or without thousands dots, in cents. No float involved."""
    match = _AMOUNT.fullmatch(text.strip())
    if match is None:
        raise ValueError("not an amount")
    sign, whole, cents = match.groups()
    value = int(whole.replace(".", "")) * 100 + int(cents)
    return -value if sign else value


def read_statement(path: Path) -> Statement:
    """The statement's lines: its purchases, and any credit without a matching purchase. The
    card payment and refunded purchases, with their refunds, are left out."""
    rows = _read_rows(path)
    left_out = [r.left_out("zero") for r in rows if r.amount_cents == 0]
    rows = [r for r in rows if r.amount_cents != 0]
    left_out += [r.left_out("card payment") for r in rows if _is_payment(r)]
    purchases = [r for r in rows if r.amount_cents < 0]
    refunds = [r for r in rows if r.amount_cents >= 0 and not _is_payment(r)]
    credits = []
    for refund in refunds:
        purchase = _refunded(refund, purchases)
        if purchase is None:
            # Its purchase was on an earlier statement: a line of its own.
            credits.append(refund)
            continue
        purchases.remove(purchase)
        left_out += [purchase.left_out("refunded"), refund.left_out("refund")]
    kept = sorted(purchases + credits, key=lambda r: r.row)
    if not kept:
        raise StatementError("the statement has no purchases to book")
    lines = [
        StatementLine(
            number=number,
            transaction_date=r.transaction_date,
            booking_date=r.booking_date,
            description=r.description,
            amount_cents=r.amount_cents,
        )
        for number, r in enumerate(kept, start=1)
    ]
    return Statement(lines=lines, left_out=left_out)


def _read_rows(path: Path) -> list[_Row]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise StatementError(f"{path.name} doesn't exist") from None
    except UnicodeDecodeError:
        raise StatementError(f"{path.name} is not UTF-8") from None
    except OSError:
        raise StatementError(f"{path.name} can't be read") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise StatementError(f"{path.name} has no column {', '.join(missing)}")
    rows = []
    number = 0
    for record in reader:
        if not any((value or "").strip() for value in record.values() if isinstance(value, str)):
            continue
        number += 1
        try:
            rows.append(
                _Row(
                    row=number,
                    transaction_date=date.fromisoformat(record["Transactiedatum"]),
                    booking_date=date.fromisoformat(record["Boekingsdatum"]),
                    description=" ".join(record["Omschrijving"].split()),
                    amount_cents=parse_amount(record["Bedrag in EUR"]),
                )
            )
        except ValueError, TypeError, AttributeError:
            raise StatementError(f"row {number} has an unreadable date or amount") from None
    return rows


def _is_payment(row: _Row) -> bool:
    return row.amount_cents >= 0 and row.description.upper().startswith(PAYMENT_PREFIX)


def _refunded(refund: _Row, purchases: list[_Row]) -> _Row | None:
    """The purchase a refund cancels: same amount, preferably the same merchant. None when
    the file has no purchase of that amount."""
    candidates = [p for p in purchases if p.amount_cents == -refund.amount_cents]
    if not candidates:
        return None
    name = normalize_name(refund.description)
    same = [c for c in candidates if normalize_name(c.description) == name]
    if same:
        return same[0]
    if len({normalize_name(c.description) for c in candidates}) == 1:
        return candidates[0]
    raise StatementError(
        f"row {refund.row} is a refund that matches purchases from more than one merchant"
    )
