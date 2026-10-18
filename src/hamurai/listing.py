"""The learned suggestions as a listing, grouped from one of several perspectives. Pure logic.

Each entry is listed once, under its current choice (the pair it is proposed with), with what it
was learned from. Patterns and provider IBANs are listed apart: they are rules, not
counterparties.
"""

from collections.abc import Callable
from datetime import date
from typing import Literal

from pydantic import BaseModel

from hamurai.matcher import Labels
from hamurai.suggestions import Entry, Pair, Suggestions, normalize_iban, normalize_name

type GroupBy = Literal["category", "vat", "match"]
# The `match` groups, in the order they are listed.
MATCH_GROUPS = ("always ask", "by IBAN", "by name")
# Entries shown per group by default: the whole listing is too large for one tool answer.
LIMIT = 10


class ListedEntry(BaseModel):
    name: str | None
    iban: str | None
    category_id: int
    vat_code_id: int
    label: str
    bookings: int
    last_seen: date
    always_ask: bool
    # Booked another way before: each other pair with its count, most used first.
    other_pairs: list[str]


class Group(BaseModel):
    title: str
    entries: list[ListedEntry]
    # Entries left out by the limit.
    more: int


class ListedPattern(BaseModel):
    contains: str
    category_id: int
    vat_code_id: int
    label: str


class SuggestionListing(BaseModel):
    group_by: GroupBy
    groups: list[Group]
    patterns: list[ListedPattern]
    provider_ibans: list[str]
    entries: int


def list_suggestions(
    suggestions: Suggestions,
    labels: Labels,
    group_by: GroupBy = "category",
    text: str | None = None,
    category_id: int | None = None,
    limit: int | None = LIMIT,
) -> SuggestionListing:
    """Every entry once, grouped; `text` (a name or an IBAN) and `category_id` narrow it.

    Each group keeps its `limit` busiest entries and counts the rest; None keeps them all.
    """
    title_of = _titles(labels).get(group_by)
    if title_of is None:
        raise ValueError(f"group_by must be one of {', '.join(_titles(labels))}")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1, or None for every entry")
    entries = [
        e
        for e in suggestions.entries
        if _matches(e, text) and (category_id is None or e.current.category_id == category_id)
    ]
    grouped: dict[str, list[ListedEntry]] = {}
    for e in entries:
        grouped.setdefault(title_of(e), []).append(_listed(e, labels))
    order = MATCH_GROUPS if group_by == "match" else sorted(grouped)
    # The busiest counterparty first, then by name.
    for listed in grouped.values():
        listed.sort(key=_busiest_first)
    groups = [
        Group(title=title, entries=grouped[title][:limit], more=len(grouped[title][limit:]))
        for title in order
        if title in grouped
    ]
    patterns = [
        ListedPattern(
            contains=p.contains,
            category_id=p.category_id,
            vat_code_id=p.vat_code_id,
            label=labels.pair(Pair(category_id=p.category_id, vat_code_id=p.vat_code_id)),
        )
        for p in suggestions.patterns
        if (text is None or text.casefold() in p.contains.casefold())
        and (category_id is None or p.category_id == category_id)
    ]
    return SuggestionListing(
        group_by=group_by,
        groups=groups,
        patterns=patterns,
        provider_ibans=suggestions.provider_ibans,
        entries=len(entries),
    )


def _titles(labels: Labels) -> dict[str, Callable[[Entry], str]]:
    return {
        "category": lambda e: labels.pair(e.current),
        "vat": lambda e: labels.vat_codes.get(e.current.vat_code_id, f"#{e.current.vat_code_id}"),
        "match": lambda e: (
            "always ask" if e.always_ask else "by IBAN" if e.iban is not None else "by name"
        ),
    }


def _matches(entry: Entry, text: str | None) -> bool:
    if text is None:
        return True
    name = normalize_name(text) or ""
    iban = normalize_iban(text)
    return name in (normalize_name(entry.name) or "") or (
        iban is not None and entry.iban is not None and iban in entry.iban
    )


def _listed(entry: Entry, labels: Labels) -> ListedEntry:
    current = entry.current
    others = sorted(
        ((count, pair) for pair, count in entry.counts.items() if pair != current),
        key=lambda item: -item[0],
    )
    return ListedEntry(
        name=entry.name,
        iban=entry.iban,
        category_id=current.category_id,
        vat_code_id=current.vat_code_id,
        label=labels.pair(current),
        bookings=len(entry.bookings),
        last_seen=entry.last_seen,
        always_ask=entry.always_ask,
        other_pairs=[f"{labels.pair(pair)} ({count}×)" for count, pair in others],
    )


def _busiest_first(entry: ListedEntry) -> tuple[int, str]:
    return (-entry.bookings, normalize_name(entry.name) or "")
