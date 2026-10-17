import pytest
from test_suggestions import IBAN, booking, tx

from hamurai.models import Transaction
from hamurai.seeding import Resolution, UnresolvedError, build_draft, resolve, seedable
from hamurai.suggestions import Pair

OTHER_IBAN = "NL00TEST0000000002"


def history(*rows: tuple[str | None, str | None, int]):
    """(iban, name, category) per booked transaction, one day apart."""
    return [
        (tx(i, iban=iban, name=name, day=i), booking(100 + i, i, category))
        for i, (iban, name, category) in enumerate(rows, start=1)
    ]


def state(transaction: Transaction, *, fully: bool, count: int) -> Transaction:
    return transaction.model_copy(
        update={"is_fully_reconciled": fully, "number_of_reconciliations": count}
    )


def test_seedable_skips_split_partly_booked_unbooked_and_anonymous():
    plain = state(tx(1), fully=True, count=1)
    split = state(tx(2), fully=True, count=2)
    partly = state(tx(3), fully=False, count=1)
    unbooked = state(tx(4), fully=False, count=0)
    anonymous = state(tx(5, iban="", name=None), fully=True, count=1)

    assert seedable([plain, split, partly, unbooked, anonymous]) == [plain]


def test_groups_by_iban_then_name():
    draft = build_draft(
        history(
            (IBAN, "Acme B.V.", 10),
            (IBAN, "ACME BV", 10),
            (None, "Bakker", 11),
            (None, " bakker ", 11),
        )
    )

    assert [(d.key, len(d.bookings)) for d in draft] == [
        (f"iban:{IBAN}", 2),
        ("name:bakker", 2),
    ]


def test_inconsistent_counterparty_is_flagged_and_listed_first():
    draft = build_draft(
        history(
            (OTHER_IBAN, "Steady", 10),
            (OTHER_IBAN, "Steady", 10),
            (OTHER_IBAN, "Steady", 10),
            (IBAN, "Mixed", 10),
            (IBAN, "Mixed", 11),
        )
    )

    assert [(d.key, d.flagged) for d in draft] == [
        (f"iban:{IBAN}", True),
        (f"iban:{OTHER_IBAN}", False),
    ]


def test_resolve_keeps_only_the_chosen_pair():
    draft = build_draft(history(*[(IBAN, "Mixed", 10)] * 5, *[(IBAN, "Mixed", 11)] * 2))

    suggestions = resolve(draft, [Resolution(key=f"iban:{IBAN}", category_id=10, vat_code_id=20)])

    [entry] = suggestions.entries
    assert entry.counts == {Pair(category_id=10, vat_code_id=20): 5}
    assert entry.current == Pair(category_id=10, vat_code_id=20)


def test_resolve_keeps_unflagged_entries_as_they_are():
    draft = build_draft(history((IBAN, "Steady", 10), (IBAN, "Steady", 10)))

    [entry] = resolve(draft, []).entries

    assert len(entry.bookings) == 2


def test_resolve_refuses_unresolved_flags():
    draft = build_draft(history((IBAN, "Mixed", 10), (IBAN, "Mixed", 11)))

    with pytest.raises(UnresolvedError) as err:
        resolve(draft, [])

    assert err.value.keys == [f"iban:{IBAN}"]


def test_resolve_refuses_pair_outside_history():
    draft = build_draft(history((IBAN, "Mixed", 10), (IBAN, "Mixed", 11)))

    with pytest.raises(ValueError, match="not in its history"):
        resolve(draft, [Resolution(key=f"iban:{IBAN}", category_id=12, vat_code_id=20)])


def test_resolve_refuses_unknown_key():
    draft = build_draft(history((IBAN, "Steady", 10)))

    with pytest.raises(ValueError, match="iban:NOPE"):
        resolve(draft, [Resolution(key="iban:NOPE", category_id=10, vat_code_id=20)])


def test_always_ask_keeps_the_whole_history():
    draft = build_draft(history((IBAN, "Mixed", 10), (IBAN, "Mixed", 11), (IBAN, "Mixed", 10)))

    [entry] = resolve(draft, [Resolution(key=f"iban:{IBAN}", always_ask=True)]).entries

    assert entry.always_ask is True
    assert len(entry.bookings) == 3


def test_resolution_needs_a_pair_or_always_ask():
    with pytest.raises(ValueError, match="always_ask"):
        Resolution(key=f"iban:{IBAN}", category_id=10)
