"""The learned bookings in `data/suggestions.yaml`: the only unit that touches that file.

Each entry is one counterparty with every booking learned for it, in the order they were
learned. The latest booking's category and VAT code is the entry's current choice.
"""

import os
import tempfile
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from hamurai.config import ROOT
from hamurai.models import Booking, Transaction

SUGGESTIONS_PATH = ROOT / "data" / "suggestions.yaml"


class SuggestionsFileError(Exception):
    """The suggestions file can't be read. It is never overwritten in that state."""


def normalize_iban(value: str | None) -> str | None:
    return "".join((value or "").split()).upper() or None


def normalize_name(value: str | None) -> str | None:
    return " ".join((value or "").split()).casefold() or None


class Pair(BaseModel, frozen=True):
    category_id: int
    vat_code_id: int


class LearnedBooking(BaseModel):
    id: int
    category_id: int
    vat_code_id: int
    date: date

    @property
    def pair(self) -> Pair:
        return Pair(category_id=self.category_id, vat_code_id=self.vat_code_id)


class Entry(BaseModel):
    iban: str | None = None
    name: str | None = None
    # Never confident: for counterparties without one right category, such as an own account.
    always_ask: bool = False
    bookings: list[LearnedBooking]

    @property
    def current(self) -> Pair:
        return self.bookings[-1].pair

    @property
    def since(self) -> date:
        """The date of the first booking in the trailing run of the current pair."""
        since = self.bookings[-1].date
        for learned in reversed(self.bookings):
            if learned.pair != self.current:
                break
            since = learned.date
        return since

    @property
    def last_seen(self) -> date:
        return max(learned.date for learned in self.bookings)

    @property
    def counts(self) -> dict[Pair, int]:
        counts: dict[Pair, int] = {}
        for learned in self.bookings:
            counts[learned.pair] = counts.get(learned.pair, 0) + 1
        return counts


class Pattern(BaseModel):
    """A hand-written rule: a description containing `contains` gets this pair."""

    contains: str
    category_id: int
    vat_code_id: int


class Suggestions(BaseModel):
    entries: list[Entry] = []
    patterns: list[Pattern] = []

    def by_iban(self, iban: str | None) -> Entry | None:
        key = normalize_iban(iban)
        if key is None:
            return None
        return next((e for e in self.entries if e.iban == key), None)

    def by_name(self, name: str | None) -> Entry | None:
        key = normalize_name(name)
        if key is None:
            return None
        return next((e for e in self.entries if normalize_name(e.name) == key), None)

    def record(self, transaction: Transaction, booking: Booking) -> bool:
        """Learn a booking. False when the transaction has neither an IBAN nor a name."""
        return self.learn(
            iban=transaction.counter_account.number,
            name=transaction.counter_account.name_account_holder,
            booking=booking,
            on=transaction.entry_date,
        )

    def learn(self, *, iban: str | None, name: str | None, booking: Booking, on: date) -> bool:
        """Learn a booking by IBAN and name, such as a card line's merchant. False without
        either."""
        iban = normalize_iban(iban)
        if iban is None and normalize_name(name) is None:
            return False
        entry = self.by_iban(iban)
        if entry is None:
            by_name = self.by_name(name)
            # A name match only joins an entry that has no other IBAN of its own.
            if by_name is not None and (iban is None or by_name.iban is None):
                entry = by_name
        if entry is None:
            entry = Entry(iban=iban, name=name, bookings=[])
            self.entries.append(entry)
        entry.iban = entry.iban or iban
        entry.name = name or entry.name
        entry.bookings.append(
            LearnedBooking(
                id=booking.id,
                category_id=booking.category_id,
                vat_code_id=booking.vat_code_id,
                date=on,
            )
        )
        return True

    def forget(self, booking_id: int) -> bool:
        """Take back a learned booking. False when it was never learned."""
        for entry in self.entries:
            for learned in entry.bookings:
                if learned.id == booking_id:
                    entry.bookings.remove(learned)
                    if not entry.bookings:
                        self.entries.remove(entry)
                    return True
        return False


def load(path: Path = SUGGESTIONS_PATH) -> Suggestions:
    """Read the suggestions. A missing file gives empty suggestions."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return Suggestions()
    try:
        data = yaml.safe_load(text)
    except yaml.MarkedYAMLError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark else "?"
        raise SuggestionsFileError(
            f"{path.name} is not valid YAML at line {line}: {exc.problem}"
        ) from None
    try:
        return Suggestions.model_validate(data or {})
    except ValidationError as exc:
        # Field locations only: pydantic's own message quotes the file's values.
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        raise SuggestionsFileError(f"{path.name} has invalid fields: {', '.join(fields)}") from None


def save(suggestions: Suggestions, path: Path = SUGGESTIONS_PATH) -> None:
    """Write the suggestions atomically: a crash never leaves a half-written file."""
    # Lists of plain values (each learned booking) become one-line flow mappings.
    data = suggestions.model_dump()
    for entry in data["entries"]:
        if not entry["always_ask"]:
            del entry["always_ask"]
    text = yaml.safe_dump(data, sort_keys=False, default_flow_style=None, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as temporary:
        temporary.write(text)
    try:
        os.replace(temporary.name, path)
    except BaseException:
        Path(temporary.name).unlink(missing_ok=True)
        raise
