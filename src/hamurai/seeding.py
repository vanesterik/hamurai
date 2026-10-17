"""Building the suggestions from the booking history, once. Pure: no network, no file.

Grouping reuses `Suggestions.record`, so the history is grouped exactly the way later
bookings are learned.
"""

from collections.abc import Sequence

from pydantic import BaseModel

from hamurai.models import Booking, Transaction
from hamurai.suggestions import (
    Entry,
    LearnedBooking,
    Pair,
    Suggestions,
    normalize_iban,
    normalize_name,
)


class DraftEntry(BaseModel):
    key: str
    iban: str | None
    name: str | None
    bookings: list[LearnedBooking]
    flagged: bool


class Resolution(BaseModel):
    """The user's choice for a flagged counterparty."""

    key: str
    category_id: int
    vat_code_id: int


class UnresolvedError(Exception):
    def __init__(self, keys: list[str]) -> None:
        super().__init__(f"Flagged counterparties without a resolution: {', '.join(keys)}")
        self.keys = keys


def seedable(transactions: Sequence[Transaction]) -> list[Transaction]:
    """Fully booked with exactly one booking, and with an IBAN or a name to learn it by."""
    return [
        t
        for t in transactions
        if t.is_fully_reconciled
        and t.number_of_reconciliations == 1
        and (
            normalize_iban(t.counter_account.number)
            or normalize_name(t.counter_account.name_account_holder)
        )
    ]


def build_draft(history: Sequence[tuple[Transaction, Booking]]) -> list[DraftEntry]:
    """Group the booked history by counterparty. Flagged ones first, then the busiest."""
    grouped = Suggestions()
    for transaction, booking in sorted(history, key=lambda row: row[0].entry_date):
        grouped.record(transaction, booking)
    draft = [
        DraftEntry(
            key=_key(entry),
            iban=entry.iban,
            name=entry.name,
            bookings=entry.bookings,
            flagged=len(entry.counts) > 1,
        )
        for entry in grouped.entries
    ]
    return sorted(draft, key=lambda d: (not d.flagged, -len(d.bookings)))


def resolve(draft: Sequence[DraftEntry], resolutions: Sequence[Resolution]) -> Suggestions:
    """Apply the user's choices: a flagged counterparty keeps only its chosen pair."""
    by_key = {d.key: d for d in draft}
    chosen: dict[str, Pair] = {}
    for resolution in resolutions:
        entry = by_key.get(resolution.key)
        if entry is None:
            raise ValueError(f"No counterparty {resolution.key} in the draft")
        pair = Pair(category_id=resolution.category_id, vat_code_id=resolution.vat_code_id)
        if all(learned.pair != pair for learned in entry.bookings):
            raise ValueError(f"The chosen pair for {resolution.key} is not in its history")
        chosen[resolution.key] = pair
    unresolved = [d.key for d in draft if d.flagged and d.key not in chosen]
    if unresolved:
        raise UnresolvedError(unresolved)
    entries = []
    for d in draft:
        pair = chosen.get(d.key)
        bookings = [b for b in d.bookings if pair is None or b.pair == pair]
        entries.append(Entry(iban=d.iban, name=d.name, bookings=bookings))
    return Suggestions(entries=entries)


def _key(entry: Entry) -> str:
    if entry.iban is not None:
        return f"iban:{entry.iban}"
    return f"name:{normalize_name(entry.name)}"
