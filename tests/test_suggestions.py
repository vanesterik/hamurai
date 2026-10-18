from datetime import date

import pytest
import yaml

from hamurai.models import Booking, Transaction
from hamurai.suggestions import (
    Entry,
    LearnedBooking,
    Pair,
    Suggestions,
    SuggestionsFileError,
    load,
    normalize_iban,
    normalize_name,
    save,
)

IBAN = "NL00TEST0000000001"


def tx(id: int = 1, *, iban: str | None = IBAN, name: str | None = "Acme B.V.", day: int = 1):
    return Transaction.model_validate(
        {
            "id": id,
            "entryDate": f"2026-07-{day:02d}",
            "amount": -1000,
            "counterAccount": {"number": iban, "nameAccountHolder": name},
            "isFullyReconciled": True,
            "numberOfReconciliations": 1,
        }
    )


def booking(id: int, transaction_id: int = 1, category_id: int = 10, vat_code_id: int = 20):
    return Booking.model_validate(
        {
            "id": id,
            "transactionInfo": {"id": transaction_id},
            "generalLedgerAccountInfo": {"id": category_id},
            "vatCode": {"id": vat_code_id},
            "amountInCents": -1000,
        }
    )


def learned(*categories: int) -> Suggestions:
    """One counterparty, booked once to each given category, on consecutive days."""
    suggestions = Suggestions()
    for i, category in enumerate(categories, start=1):
        suggestions.record(tx(i, day=i), booking(100 + i, i, category))
    return suggestions


def test_normalize_iban():
    assert normalize_iban(" nl00 test 0000 0000 01 ") == IBAN
    assert normalize_iban(None) is None


def test_normalize_name():
    assert normalize_name("  ACME   b.v. ") == "acme b.v."


def test_empty_strings_are_not_keys():
    assert normalize_iban("") is None
    assert normalize_name("  ") is None
    suggestions = Suggestions()

    assert suggestions.record(tx(iban="", name=""), booking(1)) is False
    assert suggestions.entries == []


def test_record_appends_to_existing_entry_by_iban():
    suggestions = learned(10)

    assert suggestions.record(tx(2, iban="nl00 test 0000 0000 01", name="ACME"), booking(2, 2))

    [entry] = suggestions.entries
    assert entry.counts == {Pair(category_id=10, vat_code_id=20): 2}
    assert entry.name == "ACME"


def test_record_falls_back_to_name():
    suggestions = Suggestions()
    suggestions.record(tx(1, iban=None), booking(1, 1))
    suggestions.record(tx(2, iban=None, name=" acme  B.V."), booking(2, 2))

    [entry] = suggestions.entries
    assert len(entry.bookings) == 2
    assert suggestions.by_name("ACME B.V.") is entry


def test_name_entry_takes_on_a_new_iban():
    suggestions = Suggestions()
    suggestions.record(tx(1, iban=None), booking(1, 1))
    suggestions.record(tx(2), booking(2, 2))

    [entry] = suggestions.entries
    assert entry.iban == IBAN
    assert suggestions.by_iban(IBAN) is entry


def test_latest_choice_wins():
    suggestions = learned(10, 10, 10, 11)

    [entry] = suggestions.entries
    assert entry.current == Pair(category_id=11, vat_code_id=20)
    assert entry.since == date(2026, 7, 4)
    assert entry.last_seen == date(2026, 7, 4)


def test_forget_takes_back_count_and_restores_current():
    suggestions = learned(10, 10, 10, 11)

    assert suggestions.forget(104) is True

    [entry] = suggestions.entries
    assert entry.current == Pair(category_id=10, vat_code_id=20)
    assert entry.counts == {Pair(category_id=10, vat_code_id=20): 3}


def test_forget_last_booking_drops_the_entry():
    suggestions = learned(10)

    assert suggestions.forget(101) is True
    assert suggestions.entries == []


def test_forget_unknown_booking_returns_false():
    assert learned(10).forget(999) is False


def test_missing_file_gives_empty_suggestions(tmp_path):
    assert load(tmp_path / "suggestions.yaml") == Suggestions()


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "data" / "suggestions.yaml"
    suggestions = learned(10, 11)

    save(suggestions, path)

    assert load(path) == suggestions


def test_saved_file_has_one_line_per_booking(tmp_path):
    path = tmp_path / "suggestions.yaml"

    save(learned(10, 10, 11), path)

    assert path.read_text().count("{id: ") == 3


def test_corrupt_file_reports_line_and_is_left_alone(tmp_path):
    path = tmp_path / "suggestions.yaml"
    original = "entries:\n  - iban: [unclosed\n"
    path.write_text(original)

    with pytest.raises(SuggestionsFileError, match="line"):
        load(path)

    assert path.read_text() == original


def test_invalid_contents_name_the_field(tmp_path):
    path = tmp_path / "suggestions.yaml"
    path.write_text("entries:\n  - iban: X\n    name: Y\n    bookings: [{id: notanumber}]\n")

    with pytest.raises(SuggestionsFileError, match="entries.0.bookings.0.id"):
        load(path)


def test_save_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "suggestions.yaml"
    save(learned(10), path)
    original = path.read_text()

    def broken_dump(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(yaml, "safe_dump", broken_dump)

    with pytest.raises(RuntimeError):
        save(learned(11), path)

    assert path.read_text() == original
    assert [p.name for p in tmp_path.iterdir()] == ["suggestions.yaml"]


def test_always_ask_survives_learning_and_the_file(tmp_path):
    suggestions = learned(10)
    suggestions.entries[0].always_ask = True
    suggestions.record(tx(2, day=2), booking(102, 2, 11))
    path = tmp_path / "suggestions.yaml"

    save(suggestions, path)

    [entry] = load(path).entries
    assert entry.always_ask is True
    assert len(entry.bookings) == 2


def test_always_ask_is_left_out_of_the_file_when_false(tmp_path):
    path = tmp_path / "suggestions.yaml"

    save(learned(10), path)

    assert "always_ask" not in path.read_text()


def test_learn_by_name_only():
    suggestions = Suggestions()

    assert suggestions.learn(
        iban=None, name="ACME SOFTWARE", booking=booking(7), on=date(2026, 8, 30)
    )

    entry = suggestions.by_name("acme software")
    assert entry is not None and entry.iban is None
    assert [(b.id, b.category_id, b.vat_code_id, b.date) for b in entry.bookings] == [
        (7, 10, 20, date(2026, 8, 30))
    ]
    assert suggestions.forget(7)
    assert suggestions.by_name("acme software") is None


def test_learn_without_iban_or_name_is_refused():
    assert not Suggestions().learn(iban=None, name=" ", booking=booking(7), on=date(2026, 8, 30))


PROVIDER = "NL00PROV0000000001"


def on_provider(id: int, name: str | None, day: int = 1):
    return tx(id, iban=PROVIDER, name=name, day=day)


def test_provider_ibans_are_normalised():
    suggestions = Suggestions(provider_ibans=["nl00 prov 0000 0000 01"])

    assert suggestions.is_provider(PROVIDER)
    assert not suggestions.is_provider(IBAN)
    assert not suggestions.is_provider(None)


def test_provider_ibans_survive_a_save(tmp_path):
    path = tmp_path / "suggestions.yaml"
    save(Suggestions(provider_ibans=[PROVIDER]), path)

    assert load(path).provider_ibans == [PROVIDER]


def test_no_provider_ibans_are_not_written(tmp_path):
    path = tmp_path / "suggestions.yaml"
    save(Suggestions(), path)

    assert "provider_ibans" not in path.read_text()


def test_two_merchants_on_a_provider_iban_are_two_entries():
    suggestions = Suggestions(provider_ibans=[PROVIDER])

    suggestions.record(on_provider(1, "Shop A"), booking(101, 1, 10))
    suggestions.record(on_provider(2, "Shop B", day=2), booking(102, 2, 11))

    assert [(e.iban, e.name, e.current.category_id) for e in suggestions.entries] == [
        (None, "Shop A", 10),
        (None, "Shop B", 11),
    ]


def test_provider_transaction_without_name_is_not_learned():
    suggestions = Suggestions(provider_ibans=[PROVIDER])

    assert not suggestions.record(on_provider(1, None), booking(101))
    assert suggestions.entries == []


def test_old_provider_entry_never_answers_a_name_lookup():
    old = Entry(
        iban=PROVIDER,
        name="Shop A",
        bookings=[LearnedBooking(id=1, category_id=11, vat_code_id=20, date=date(2026, 1, 1))],
    )
    suggestions = Suggestions(entries=[old], provider_ibans=[PROVIDER])

    assert suggestions.by_name("Shop A") is None
    suggestions.record(on_provider(2, "Shop A"), booking(102, 2, 10))
    assert [(e.iban, len(e.bookings)) for e in suggestions.entries] == [(PROVIDER, 1), (None, 1)]


def test_sort_bookings_makes_the_latest_current():
    entry = Entry(
        name="Shop A",
        bookings=[
            LearnedBooking(id=2, category_id=10, vat_code_id=20, date=date(2026, 5, 1)),
            LearnedBooking(id=1, category_id=11, vat_code_id=20, date=date(2025, 5, 1)),
        ],
    )

    entry.sort_bookings()

    assert [b.id for b in entry.bookings] == [1, 2]
    assert entry.current.category_id == 10


def test_provider_ibans_are_normalised_when_read(tmp_path):
    path = tmp_path / "suggestions.yaml"
    path.write_text("provider_ibans:\n- nl00 prov 0000 0000 01\n- NL00PROV0000000001\n")

    assert load(path).provider_ibans == [PROVIDER]


def test_current_is_the_latest_dated_booking_not_the_last_learned():
    entry = Entry(
        name="Own account",
        bookings=[
            LearnedBooking(id=1, category_id=10, vat_code_id=20, date=date(2026, 9, 1)),
            LearnedBooking(id=2, category_id=10, vat_code_id=20, date=date(2026, 10, 16)),
            # Learned last, but dated earlier.
            LearnedBooking(id=3, category_id=11, vat_code_id=20, date=date(2026, 10, 2)),
        ],
    )

    assert entry.current.category_id == 10
    assert entry.latest.id == 2
    assert entry.since == date(2026, 10, 16)


def test_on_the_same_date_the_booking_learned_last_wins():
    entry = Entry(
        name="Shop",
        bookings=[
            LearnedBooking(id=1, category_id=10, vat_code_id=20, date=date(2026, 10, 2)),
            LearnedBooking(id=2, category_id=11, vat_code_id=20, date=date(2026, 10, 2)),
        ],
    )

    assert entry.current.category_id == 11
    assert entry.since == date(2026, 10, 2)
