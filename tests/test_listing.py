from datetime import date

import pytest

from hamurai.listing import list_suggestions
from hamurai.matcher import Labels
from hamurai.suggestions import Entry, LearnedBooking, Pattern, Suggestions

LABELS = Labels(
    categories={10: "4610 Contributies", 11: "4605 Automatisering", 12: "1700 Rekening courant"},
    vat_codes={20: "Hoog 21%", 21: "Geen"},
)
PROVIDER = "NL00PROV0000000001"


def entry(name: str, iban: str | None, *pairs: tuple[int, int, str], **kwargs) -> Entry:
    return Entry(
        name=name,
        iban=iban,
        bookings=[
            LearnedBooking(id=i, category_id=c, vat_code_id=v, date=date.fromisoformat(d))
            for i, (c, v, d) in enumerate(pairs, start=1)
        ],
        **kwargs,
    )


SUGGESTIONS = Suggestions(
    entries=[
        entry("Spotify", None, (10, 21, "2026-08-01"), (10, 21, "2026-09-01")),
        entry("NRC", "NL00TEST0000000001", (10, 20, "2026-09-14")),
        entry(
            "Nebius", None, (11, 20, "2026-07-01"), (11, 20, "2026-07-02"), (11, 20, "2026-07-03")
        ),
        entry(
            "Own account",
            "NL00TEST0000000002",
            (12, 21, "2026-09-01"),
            (10, 20, "2026-10-02"),
            always_ask=True,
        ),
    ],
    patterns=[Pattern(contains="Spotify", category_id=10, vat_code_id=21)],
    provider_ibans=[PROVIDER],
)


def titles(listing) -> list[str]:
    return [g.title for g in listing.groups]


def names(group) -> list[str]:
    return [e.name for e in group.entries]


def test_by_category_and_vat_code():
    listing = list_suggestions(SUGGESTIONS, LABELS)

    assert listing.group_by == "category"
    assert titles(listing) == [
        "4605 Automatisering / Hoog 21%",
        "4610 Contributies / Geen",
        "4610 Contributies / Hoog 21%",
    ]
    assert [names(g) for g in listing.groups] == [["Nebius"], ["Spotify"], ["Own account", "NRC"]]
    assert listing.entries == 4


def test_each_entry_shows_its_counts_and_marks():
    listing = list_suggestions(SUGGESTIONS, LABELS)
    own = next(e for g in listing.groups for e in g.entries if e.name == "Own account")

    assert (own.bookings, own.last_seen, own.always_ask) == (2, date(2026, 10, 2), True)
    assert own.iban == "NL00TEST0000000002"
    assert own.other_pairs == ["1700 Rekening courant / Geen (1×)"]


def test_busiest_counterparty_first_within_a_group():
    nrc_twice = SUGGESTIONS.model_copy(deep=True)
    nrc_twice.entries[1].bookings.append(
        LearnedBooking(id=9, category_id=10, vat_code_id=20, date=date(2026, 10, 14))
    )
    nrc_twice.entries[1].bookings.append(
        LearnedBooking(id=10, category_id=10, vat_code_id=20, date=date(2026, 10, 15))
    )

    listing = list_suggestions(nrc_twice, LABELS)

    assert names(listing.groups[2]) == ["NRC", "Own account"]


def test_by_vat_code():
    listing = list_suggestions(SUGGESTIONS, LABELS, group_by="vat")

    assert titles(listing) == ["Geen", "Hoog 21%"]
    assert [names(g) for g in listing.groups] == [["Spotify"], ["Nebius", "Own account", "NRC"]]


def test_by_how_a_counterparty_is_matched():
    listing = list_suggestions(SUGGESTIONS, LABELS, group_by="match")

    assert titles(listing) == ["always ask", "by IBAN", "by name"]
    assert [names(g) for g in listing.groups] == [["Own account"], ["NRC"], ["Nebius", "Spotify"]]


def test_patterns_and_provider_ibans_are_listed_apart():
    listing = list_suggestions(SUGGESTIONS, LABELS)

    assert [(p.contains, p.label) for p in listing.patterns] == [
        ("Spotify", "4610 Contributies / Geen")
    ]
    assert listing.provider_ibans == [PROVIDER]


def test_text_filter_on_name_iban_and_pattern():
    listing = list_suggestions(SUGGESTIONS, LABELS, text="spot")

    assert [names(g) for g in listing.groups] == [["Spotify"]]
    assert [p.contains for p in listing.patterns] == ["Spotify"]
    assert listing.entries == 1

    by_iban = list_suggestions(SUGGESTIONS, LABELS, text="nl00 test 0000 0000 01")
    assert [names(g) for g in by_iban.groups] == [["NRC"]]


def test_category_filter():
    listing = list_suggestions(SUGGESTIONS, LABELS, category_id=10)

    assert titles(listing) == ["4610 Contributies / Geen", "4610 Contributies / Hoog 21%"]
    assert [p.contains for p in listing.patterns] == ["Spotify"]


def test_unknown_grouping_is_refused():
    with pytest.raises(ValueError):
        list_suggestions(SUGGESTIONS, LABELS, group_by="colour")  # ty: ignore[invalid-argument-type]


def test_limit_keeps_the_busiest_and_counts_the_rest():
    listing = list_suggestions(SUGGESTIONS, LABELS, group_by="vat", limit=2)

    hoog = listing.groups[1]
    assert names(hoog) == ["Nebius", "Own account"]
    assert hoog.more == 1
    assert listing.groups[0].more == 0
    assert listing.entries == 4


def test_ten_per_group_by_default_and_none_for_all():
    many = Suggestions(
        entries=[entry(f"Shop {i:02}", None, (10, 20, "2026-09-01")) for i in range(12)]
    )

    assert len(list_suggestions(many, LABELS).groups[0].entries) == 10
    assert list_suggestions(many, LABELS).groups[0].more == 2
    assert len(list_suggestions(many, LABELS, limit=None).groups[0].entries) == 12


def test_limit_below_one_is_refused():
    with pytest.raises(ValueError):
        list_suggestions(SUGGESTIONS, LABELS, limit=0)
