from datetime import date

import pytest
from test_suggestions import IBAN, booking, tx

from hamurai.matcher import Confidence, Labels, propose, propose_all, propose_line
from hamurai.models import Transaction
from hamurai.statement import StatementLine
from hamurai.suggestions import Entry, LearnedBooking, Pattern, Suggestions

LABELS = Labels(
    categories={10: "4500 Kantoorkosten", 11: "4600 Reiskosten"},
    vat_codes={20: "21% hoog"},
)


def entry(iban: str | None, name: str, *runs: tuple[int, int, str]) -> Entry:
    """Runs of (count, category, first date), learned in this order, one day apart."""
    bookings = []
    for count, category, first in runs:
        start = date.fromisoformat(first)
        for i in range(count):
            bookings.append(
                LearnedBooking(
                    id=len(bookings) + 1,
                    category_id=category,
                    vat_code_id=20,
                    date=start.replace(day=start.day + i),
                )
            )
    return Entry(iban=iban, name=name, bookings=bookings)


STEADY = entry(IBAN, "Acme B.V.", (14, 10, "2026-01-01"))
CHANGED = entry("NL00TEST0000000002", "Travel Co", (14, 10, "2026-01-01"), (1, 11, "2026-10-01"))
SUGGESTIONS = Suggestions(
    entries=[STEADY, CHANGED],
    patterns=[Pattern(contains="Bankkosten", category_id=11, vat_code_id=20)],
)


def unbooked(**kwargs) -> Transaction:
    return tx(**kwargs).model_copy(
        update={"is_fully_reconciled": False, "number_of_reconciliations": 0}
    )


def with_description(transaction: Transaction, description: str) -> Transaction:
    return transaction.model_copy(update={"description": description})


@pytest.mark.parametrize(
    ("transaction", "confidence", "category", "evidence"),
    [
        (
            unbooked(iban="nl00 test 0000 0000 01", name="Someone else"),
            Confidence.CONFIDENT,
            10,
            "IBAN match, booked 14 times as 4500 Kantoorkosten / 21% hoog, last on 2026-01-14",
        ),
        (
            unbooked(iban="NL00TEST0000000002", name="Travel Co"),
            Confidence.CONFIDENT,
            11,
            "IBAN match, now 4600 Reiskosten / 21% hoog since 2026-10-01 "
            "(booked this way 1 time in all); "
            "before that 14 times as 4500 Kantoorkosten / 21% hoog",
        ),
        (
            unbooked(iban=None, name="ACME  b.v."),
            Confidence.UNCERTAIN,
            10,
            "Name match, booked 14 times as 4500 Kantoorkosten / 21% hoog, last on 2026-01-14",
        ),
        (
            with_description(unbooked(iban=None, name=None), "ING BANKKOSTEN Q3"),
            Confidence.UNCERTAIN,
            11,
            'Description contains "Bankkosten"',
        ),
        (unbooked(iban="NL00TEST0000000099", name="Nobody"), Confidence.UNKNOWN, None, "No match"),
    ],
)
def test_propose(transaction, confidence, category, evidence):
    proposal = propose(transaction, SUGGESTIONS, LABELS)

    assert proposal.confidence == confidence
    assert proposal.category_id == category
    assert proposal.vat_code_id == (None if category is None else 20)
    assert proposal.evidence == evidence


def test_iban_match_wins_over_name_of_another_entry():
    proposal = propose(unbooked(iban=IBAN, name="Travel Co"), SUGGESTIONS, LABELS)

    assert (proposal.confidence, proposal.category_id) == (Confidence.CONFIDENT, 10)


def test_older_pairs_are_listed_by_count():
    suggestions = Suggestions(
        entries=[
            entry(IBAN, "A", (2, 11, "2026-01-01"), (5, 10, "2026-02-01"), (1, 12, "2026-03-01"))
        ]
    )
    labels = Labels(categories={**LABELS.categories, 12: "4700 Overig"}, vat_codes={20: "21% hoog"})

    proposal = propose(unbooked(iban=IBAN), suggestions, labels)

    assert proposal.evidence == (
        "IBAN match, now 4700 Overig / 21% hoog since 2026-03-01 (booked this way 1 time in all); "
        "before that 5 times as 4500 Kantoorkosten / 21% hoog; "
        "2 times as 4600 Reiskosten / 21% hoog"
    )


def test_missing_label_shows_the_id():
    suggestions = Suggestions(entries=[entry(IBAN, "A", (1, 99, "2026-01-01"))])

    proposal = propose(unbooked(iban=IBAN), suggestions, LABELS)

    assert proposal.evidence == "IBAN match, booked 1 time as #99 / 21% hoog, last on 2026-01-01"


def test_propose_all_groups_and_skips():
    fully = tx(1, iban=IBAN)
    partly = unbooked(id=2, iban=IBAN).model_copy(update={"number_of_reconciliations": 1})
    confident = unbooked(id=3, iban=IBAN)
    uncertain = unbooked(id=4, iban=None, name="Acme B.V.")
    unknown = unbooked(id=5, iban=None, name="Nobody")

    result = propose_all([fully, partly, confident, uncertain, unknown], SUGGESTIONS, LABELS)

    assert [p.transaction.id for p in result.confident] == [3]
    assert [p.transaction.id for p in result.uncertain] == [4]
    assert [p.transaction.id for p in result.unknown] == [5]
    assert [t.id for t in result.skipped] == [2]


def test_learned_booking_feeds_the_matcher():
    suggestions = Suggestions()
    suggestions.record(tx(1, iban=IBAN), booking(1, 1, 10))

    assert propose(unbooked(iban=IBAN), suggestions, LABELS).confidence == Confidence.CONFIDENT


def test_always_ask_is_uncertain_with_the_latest_pair_and_history():
    mixed = entry(
        IBAN, "Own account", (2, 10, "2026-01-01"), (3, 11, "2026-02-01"), (1, 10, "2026-03-01")
    )
    mixed.always_ask = True

    proposal = propose(unbooked(iban=IBAN), Suggestions(entries=[mixed]), LABELS)

    assert proposal.confidence == Confidence.UNCERTAIN
    assert (proposal.category_id, proposal.vat_code_id) == (10, 20)
    assert proposal.evidence == (
        "IBAN match, always ask; last booked as 4500 Kantoorkosten / 21% hoog on 2026-03-01; "
        "history: 3 times as 4500 Kantoorkosten / 21% hoog; 3 times as 4600 Reiskosten / 21% hoog"
    )


def card_line(description: str) -> StatementLine:
    return StatementLine(
        number=1,
        transaction_date=date(2026, 8, 30),
        booking_date=date(2026, 8, 31),
        description=description,
        amount_cents=-1299,
    )


MERCHANT = entry(None, "Acme Software", (3, 10, "2026-01-01"))
ASK = entry(None, "Marketplace", (1, 10, "2026-01-01"), (1, 11, "2026-02-01")).model_copy(
    update={"always_ask": True}
)
CARD_SUGGESTIONS = Suggestions(
    entries=[MERCHANT, ASK],
    patterns=[Pattern(contains="cloud", category_id=11, vat_code_id=20)],
)


def test_line_name_match_is_confident():
    proposal = propose_line(card_line("Acme Software"), CARD_SUGGESTIONS, LABELS)
    assert proposal.confidence is Confidence.CONFIDENT
    assert (proposal.category_id, proposal.vat_code_id) == (10, 20)
    assert proposal.evidence.startswith("Merchant match")
    assert proposal.line.description == "Acme Software"


def test_line_name_match_ignores_case_and_spacing():
    proposal = propose_line(card_line("ACME  SOFTWARE"), CARD_SUGGESTIONS, LABELS)
    assert proposal.confidence is Confidence.CONFIDENT


def test_line_always_ask_is_uncertain():
    proposal = propose_line(card_line("MARKETPLACE"), CARD_SUGGESTIONS, LABELS)
    assert proposal.confidence is Confidence.UNCERTAIN
    assert "always ask" in proposal.evidence


def test_line_pattern_is_uncertain():
    proposal = propose_line(card_line("EXAMPLE CLOUD HOSTING"), CARD_SUGGESTIONS, LABELS)
    assert proposal.confidence is Confidence.UNCERTAIN
    assert (proposal.category_id, proposal.vat_code_id) == (11, 20)


def test_line_without_a_match_is_unknown():
    proposal = propose_line(card_line("SOMEWHERE ELSE"), CARD_SUGGESTIONS, LABELS)
    assert proposal.confidence is Confidence.UNKNOWN
    assert (proposal.category_id, proposal.vat_code_id) == (None, None)


PROVIDER = "NL00PROV0000000001"
OLD_PROVIDER_ENTRY = entry(PROVIDER, "Shop A", (3, 11, "2026-01-01"))
SHOP_A = entry(None, "Shop A", (2, 10, "2026-02-01"))
PROVIDER_SUGGESTIONS = Suggestions(
    entries=[OLD_PROVIDER_ENTRY, SHOP_A, ASK],
    patterns=[Pattern(contains="Bankkosten", category_id=11, vat_code_id=20)],
    provider_ibans=[PROVIDER],
)


def test_provider_transaction_matches_its_merchant():
    proposal = propose(unbooked(iban=PROVIDER, name="Shop A"), PROVIDER_SUGGESTIONS, LABELS)

    assert proposal.confidence is Confidence.UNCERTAIN
    assert (proposal.category_id, proposal.vat_code_id) == (10, 20)
    assert proposal.evidence.startswith("Merchant match via payment provider")


def test_provider_transaction_of_an_unknown_merchant_falls_to_patterns():
    transaction = unbooked(iban=PROVIDER, name="Shop Z").model_copy(
        update={"description": "Bankkosten"}
    )

    proposal = propose(transaction, PROVIDER_SUGGESTIONS, LABELS)

    assert proposal.confidence is Confidence.UNCERTAIN
    assert proposal.evidence.startswith("Description contains")


def test_provider_merchant_marked_always_ask_stays_uncertain():
    proposal = propose(unbooked(iban=PROVIDER, name="Marketplace"), PROVIDER_SUGGESTIONS, LABELS)

    assert proposal.confidence is Confidence.UNCERTAIN
    assert "always ask" in proposal.evidence


def test_iban_not_on_the_list_is_unchanged():
    suggestions = PROVIDER_SUGGESTIONS.model_copy(update={"provider_ibans": []})

    proposal = propose(unbooked(iban=PROVIDER, name="Shop A"), suggestions, LABELS)

    assert proposal.confidence is Confidence.CONFIDENT
    assert proposal.category_id == 11


def test_always_ask_evidence_names_the_latest_dated_booking():
    mixed = entry(IBAN, "Own account", (1, 10, "2026-10-16"), (1, 11, "2026-10-02"))
    mixed.always_ask = True

    proposal = propose(unbooked(), Suggestions(entries=[mixed]), LABELS)

    assert proposal.category_id == 10
    assert "on 2026-10-16" in proposal.evidence


def test_a_pair_booked_again_after_another_counts_all_its_bookings():
    suggestions = Suggestions(
        entries=[
            entry(IBAN, "A", (2, 11, "2026-01-01"), (1, 10, "2026-02-01"), (1, 11, "2026-03-01"))
        ]
    )

    proposal = propose(unbooked(iban=IBAN), suggestions, LABELS)

    assert proposal.evidence == (
        "IBAN match, now 4600 Reiskosten / 21% hoog since 2026-03-01 "
        "(booked this way 3 times in all); "
        "before that 1 time as 4500 Kantoorkosten / 21% hoog"
    )


def on_shared_iban(name: str) -> Suggestions:
    """One entry learned under IBAN, booked three times."""
    return Suggestions(entries=[entry(IBAN, name, (3, 10, "2026-01-01"))])


def test_another_party_on_a_learned_iban_gets_the_provider_hint():
    suggestions = on_shared_iban("Spotify AB")

    proposal = propose(unbooked(iban=IBAN, name="KLM"), suggestions, LABELS)

    assert proposal.hint == (
        f"{IBAN} may be a payment provider: list it in provider_ibans and run relearn_providers."
    )
    assert (proposal.confidence, proposal.category_id) == (Confidence.CONFIDENT, 10)
    assert proposal.evidence == (
        "IBAN match, booked 3 times as 4500 Kantoorkosten / 21% hoog, last on 2026-01-03"
    )


@pytest.mark.parametrize(
    "name",
    [
        "AXUS NEDERLAND N.V.",
        "Axus Nederland B.V.-Axus Nederland BV",
        "Axus Nederland BV",
        "AXUS",
    ],
)
def test_one_party_spelled_another_way_gets_no_hint(name):
    suggestions = on_shared_iban("Axus Nederland B.V.")

    assert propose(unbooked(iban=IBAN, name=name), suggestions, LABELS).hint is None


@pytest.mark.parametrize(
    ("learned", "arriving"), [("NRC MEDIA", "NRC"), ("Healthclub", "Helthclub")]
)
def test_close_names_get_no_hint(learned, arriving):
    suggestions = on_shared_iban(learned)

    assert propose(unbooked(iban=IBAN, name=arriving), suggestions, LABELS).hint is None


def test_a_listed_provider_iban_gets_no_hint():
    suggestions = on_shared_iban("Spotify AB")
    suggestions.provider_ibans = [IBAN]

    assert propose(unbooked(iban=IBAN, name="KLM"), suggestions, LABELS).hint is None


@pytest.mark.parametrize("name", [None, "B.V."])
def test_no_name_to_compare_gets_no_hint(name):
    suggestions = on_shared_iban("Spotify AB")

    assert propose(unbooked(iban=IBAN, name=name), suggestions, LABELS).hint is None


def test_a_name_match_gets_no_hint():
    suggestions = Suggestions(entries=[entry(None, "Spotify AB", (3, 10, "2026-01-01"))])

    assert propose(unbooked(iban=IBAN, name="Spotify AB"), suggestions, LABELS).hint is None


@pytest.mark.parametrize(
    ("learned", "arriving"),
    [
        ("Dropbox by Adyen", "KLM by Adyen"),
        ("Simon Levelt BV via Stichting Mollie Payments", "Eline Coene academy via Mollie"),
    ],
)
def test_the_provider_named_after_by_or_via_is_not_the_party(learned, arriving):
    suggestions = on_shared_iban(learned)

    assert propose(unbooked(iban=IBAN, name=arriving), suggestions, LABELS).hint is not None
