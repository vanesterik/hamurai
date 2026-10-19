"""The learned bookings in `data/suggestions.yaml`: the only unit that touches that file.

Each entry is one counterparty with every booking learned for it, in the order they were
learned. The latest-dated booking's category and VAT code is the entry's current choice.
"""

import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError, field_validator
from yaml.reader import ReaderError

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

    # Learned IBANs are stored normalised; one typed by hand in the file may not be.
    @field_validator("iban")
    @classmethod
    def _normalised(cls, iban: str | None) -> str | None:
        return normalize_iban(iban)

    @property
    def by_date(self) -> list[LearnedBooking]:
        """The bookings by date. Stable: on one date, the booking learned last comes last."""
        return sorted(self.bookings, key=lambda learned: learned.date)

    @property
    def latest(self) -> LearnedBooking:
        """The latest-dated booking, whatever order it was learned in."""
        return self.by_date[-1]

    @property
    def current(self) -> Pair:
        return self.latest.pair

    @property
    def since(self) -> date:
        """The date of the first booking in the trailing run of the current pair."""
        current = self.current
        since = self.latest.date
        for learned in reversed(self.by_date):
            if learned.pair != current:
                break
            since = learned.date
        return since

    @property
    def last_seen(self) -> date:
        return self.latest.date

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
    # Payment providers collect for many merchants under one IBAN: kept by hand, and their
    # transactions are matched and learned by the merchant's name, never by the IBAN.
    provider_ibans: list[str] = []

    @field_validator("provider_ibans")
    @classmethod
    def _normalised(cls, ibans: list[str]) -> list[str]:
        return list(dict.fromkeys(i for i in map(normalize_iban, ibans) if i is not None))

    def is_provider(self, iban: str | None) -> bool:
        key = normalize_iban(iban)
        return key is not None and key in self.provider_ibans

    def by_iban(self, iban: str | None) -> Entry | None:
        key = normalize_iban(iban)
        if key is None:
            return None
        return next((e for e in self.entries if e.iban == key), None)

    def by_name(self, name: str | None) -> Entry | None:
        key = normalize_name(name)
        if key is None:
            return None
        # An entry under a provider's own IBAN mixes merchants: it never answers for one.
        return next(
            (
                e
                for e in self.entries
                if normalize_name(e.name) == key and not self.is_provider(e.iban)
            ),
            None,
        )

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
        either. A provider's IBAN counts as none: the booking is learned by the merchant's name."""
        iban = None if self.is_provider(iban) else normalize_iban(iban)
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

    def relearn_providers(self, history: Sequence[tuple[Transaction, Booking]]) -> Relearned:
        """Rebuild what was learned on the provider IBANs from their booked history, by
        merchant; the latest choice wins. Gives the same result when run again."""
        history = sorted(history, key=lambda row: row[0].entry_date)
        relearned = {booking.id for _, booking in history}
        removed = [e for e in self.entries if self.is_provider(e.iban)]
        # Bookings in those entries that aren't in the history (a card line once learned into
        # one by its name) stay with that name rather than being lost.
        kept = [(e.name, b) for e in removed for b in e.bookings if b.id not in relearned]
        self.entries = [e for e in self.entries if not self.is_provider(e.iban)]
        # Taken back wherever they were learned before, so a second run counts nothing twice.
        # Emptied entries stay until the end, so relearning finds them with their marks.
        for entry in self.entries:
            entry.bookings = [b for b in entry.bookings if b.id not in relearned]
        touched: dict[int, Entry] = {}
        learned = 0
        for transaction, booking in history:
            if self.record(transaction, booking):
                learned += 1
                entry = self.by_name(transaction.counter_account.name_account_holder)
                if entry is not None:
                    touched[id(entry)] = entry
        for name, booking in kept:
            entry = self.by_name(name)
            if entry is None:
                entry = Entry(name=name, bookings=[])
                self.entries.append(entry)
            entry.bookings.append(booking)
            touched[id(entry)] = entry
        self.entries = [e for e in self.entries if e.bookings]
        return Relearned(
            merchants=list(touched.values()), learned=learned, kept=len(kept), removed=len(removed)
        )

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


@dataclass(frozen=True)
class Relearned:
    """What relearn_providers did: the merchants it touched, and how many bookings it learned
    and kept, and entries it removed."""

    merchants: list[Entry]
    learned: int
    kept: int
    removed: int


def load(path: Path = SUGGESTIONS_PATH) -> Suggestions:
    """Read the suggestions. A missing file gives empty suggestions."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return Suggestions()
    try:
        data = yaml.safe_load(text)
    except ReaderError as exc:
        # Its own message quotes the character; the line is enough to find it.
        line = text[: exc.position].count("\n") + 1
        raise SuggestionsFileError(
            f"{path.name} contains a character YAML doesn't allow at line {line}"
        ) from None
    except yaml.MarkedYAMLError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark else "?"
        raise SuggestionsFileError(
            f"{path.name} is not valid YAML at line {line}: {exc.problem}"
        ) from None
    except yaml.YAMLError:
        raise SuggestionsFileError(f"{path.name} is not valid YAML") from None
    try:
        return Suggestions.model_validate(data or {})
    except ValidationError as exc:
        # Field locations and lines only: pydantic's own message quotes the file's values.
        root = yaml.compose(text)
        fields = sorted(
            {
                f"{'.'.join(str(part) for part in error['loc'])} (line {_line(root, error['loc'])})"
                for error in exc.errors()
            }
        )
        raise SuggestionsFileError(f"{path.name} has invalid fields: {', '.join(fields)}") from None


def _line(node: yaml.Node | None, loc: tuple[int | str, ...]) -> int:
    """The line of the deepest node the field path reaches: a missing field gives its parent's."""
    if node is None:
        return 1
    for part in loc:
        child = None
        if isinstance(node, yaml.MappingNode):
            child = next((v for k, v in node.value if k.value == part), None)
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int):
            child = node.value[part] if part < len(node.value) else None
        if child is None:
            break
        node = child
    return node.start_mark.line + 1


def save(suggestions: Suggestions, path: Path = SUGGESTIONS_PATH) -> None:
    """Write the suggestions atomically: a crash never leaves a half-written file."""
    # Lists of plain values (each learned booking) become one-line flow mappings.
    data = suggestions.model_dump()
    for entry in data["entries"]:
        if not entry["always_ask"]:
            del entry["always_ask"]
    if not data["provider_ibans"]:
        del data["provider_ibans"]
    text = yaml.safe_dump(data, sort_keys=False, default_flow_style=None, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    # A failed write or rename removes the temporary file: nothing is left beside the file.
    try:
        with temporary:
            temporary.write(text)
        os.replace(temporary.name, path)
    except BaseException:
        Path(temporary.name).unlink(missing_ok=True)
        raise
