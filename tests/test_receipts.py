from datetime import date, timedelta

from fake_api import make_file

from hamurai.models import Booking, File, Transaction
from hamurai.receipts import match, targets

DAY = date(2026, 7, 1)


def tx(
    id: int,
    amount: int = -875,
    on: date = DAY,
    *,
    name: str | None = "Shop",
    description: str = "",
    bookings: int = 1,
    has_files: bool = False,
) -> Transaction:
    return Transaction.model_validate(
        {
            "id": id,
            "entryDate": on.isoformat(),
            "amount": amount,
            "description": description,
            "counterAccount": {"number": None, "nameAccountHolder": name},
            "isFullyReconciled": bookings > 0,
            "numberOfReconciliations": bookings,
            "hasFiles": has_files,
        }
    )


def booking(id: int, transaction_id: int, amount: int, description: str = "MERCHANT") -> Booking:
    return Booking.model_validate(
        {
            "id": id,
            "transactionInfo": {"id": transaction_id},
            "generalLedgerAccountInfo": {"id": 10},
            "vatCode": {"id": 20},
            "amountInCents": amount,
            "description": description,
        }
    )


def receipt(id: int, amount: int | None = 875, on: date | None = DAY, **kwargs) -> File:
    return File.model_validate(
        make_file(
            id,
            created_at="2026-10-01T10:00:00",
            amount_cents=amount,
            receipt_date=on.isoformat() if on else None,
            **kwargs,
        )
    )


def proposal(transactions, files, splits=None):
    return match(targets(transactions, splits or {}), files)


def test_exact_amount_in_the_window_is_confident():
    result = proposal([tx(1)], [receipt(5, on=DAY - timedelta(days=1))])

    [pair] = result.confident
    assert (pair.receipt.file_id, pair.target.transaction_id) == (5, 1)
    assert pair.receipt.amount_cents == 875 and pair.target.amount_cents == -875
    assert (result.ambiguous, result.unmatched, result.without_amount) == ([], [], 0)


def test_bank_window_edges():
    for days, matched in [(-30, True), (-31, False), (3, True), (4, False)]:
        result = proposal([tx(1)], [receipt(5, on=DAY + timedelta(days=days))])
        assert bool(result.confident) is matched, days
        assert bool(result.unmatched) is not matched, days


def test_split_line_window_edges():
    collection = tx(1, -1799, date(2026, 8, 17), bookings=2)
    splits = {1: [booking(11, 1, -1299), booking(12, 1, -500)]}
    for days, matched in [(-45, True), (-46, False), (1, False), (0, True)]:
        files = [receipt(5, 1299, date(2026, 8, 17) + timedelta(days=days))]
        result = proposal([collection], files, splits)
        assert bool(result.confident) is matched, days


def test_two_receipts_for_one_target_are_ambiguous():
    result = proposal([tx(1)], [receipt(5), receipt(6, on=DAY - timedelta(days=2))])

    assert result.confident == []
    [group] = result.ambiguous
    assert sorted(r.file_id for r in group.receipts) == [5, 6]
    assert [t.transaction_id for t in group.targets] == [1]


def test_one_receipt_for_two_targets_is_ambiguous():
    result = proposal([tx(1), tx(2, on=DAY + timedelta(days=2))], [receipt(5)])

    [group] = result.ambiguous
    assert sorted(t.transaction_id for t in group.targets) == [1, 2]


def test_identical_split_lines_take_as_many_receipts():
    collection = tx(1, -2598, date(2026, 8, 17), bookings=2)
    splits = {1: [booking(11, 1, -1299), booking(12, 1, -1299)]}
    files = [receipt(5, 1299, date(2026, 8, 1)), receipt(6, 1299, date(2026, 8, 2))]

    result = proposal([collection], files, splits)

    assert sorted(p.receipt.file_id for p in result.confident) == [5, 6]
    assert {p.target.transaction_id for p in result.confident} == {1}

    more = proposal([collection], [*files, receipt(7, 1299, date(2026, 8, 3))], splits)
    assert more.confident == [] and len(more.ambiguous) == 1


def test_income_is_not_a_target():
    result = proposal([tx(1, 875)], [receipt(5)])

    assert result.confident == [] and [r.file_id for r in result.unmatched] == [5]


def test_credit_line_is_not_a_target():
    collection = tx(1, -424, date(2026, 8, 17), bookings=2)
    splits = {1: [booking(11, 1, -500), booking(12, 1, 76)]}

    assert [t.amount_cents for t in targets([collection], splits)] == [-500]


def test_transaction_with_files_is_not_a_target():
    assert targets([tx(1, has_files=True)], {}) == []
    collection = tx(2, -1799, bookings=2, has_files=True)
    splits = {2: [booking(11, 2, -1299), booking(12, 2, -500)]}
    assert len(targets([collection], splits)) == 2


def test_linked_and_archived_files_are_ignored():
    files = [receipt(5, transaction_id=9), receipt(6, archived_at="2026-09-01T10:00:00")]

    result = proposal([tx(1)], files)

    assert (result.confident, result.ambiguous, result.unmatched) == ([], [], [])


def test_receipt_without_date_is_counted():
    files = [receipt(5, on=None), receipt(6, amount=None), receipt(7, amount=None, on=None)]

    result = proposal([tx(1)], files)

    assert result.without_amount == 3
    assert (result.confident, result.ambiguous, result.unmatched) == ([], [], [])


def test_split_line_shows_its_merchant():
    collection = tx(1, -1799, bookings=2, name="Card Company")
    splits = {1: [booking(11, 1, -1299, "ACME SOFTWARE"), booking(12, 1, -500, "SHOP")]}

    [first, _] = targets([collection], splits)

    assert (first.counterparty, first.split_line) == ("ACME SOFTWARE", True)


def test_counterparty_falls_back_to_the_description():
    [target] = targets([tx(1, name=None, description="Kosten pakket")], {})

    assert (target.counterparty, target.split_line) == ("Kosten pakket", False)


PERIOD = (date(2026, 7, 1), date(2026, 9, 30))


def unlinked(id: int, created: str, amount: int | None = None, on: str | None = None) -> File:
    return File.model_validate(
        make_file(id, created_at=f"{created}T10:00:00", amount_cents=amount, receipt_date=on)
    )


def test_unmatched_counts_only_receipts_that_could_belong_to_the_period():
    files = [
        unlinked(1, "2026-05-20", 4200, "2026-05-17"),  # 45 days before the start: counted
        unlinked(2, "2026-05-20", 4200, "2026-05-16"),  # 46 days before: not
        unlinked(3, "2026-10-03", 4200, "2026-10-03"),  # 3 days after the end: counted
        unlinked(4, "2026-10-10", 4200, "2026-10-04"),  # 4 days after: not
        unlinked(5, "2026-08-01", 4200, "2026-08-01"),  # inside: counted
    ]

    result = match([], files, PERIOD)

    assert sorted(r.file_id for r in result.unmatched) == [1, 3, 5]


def test_without_amount_counts_only_files_uploaded_around_the_period():
    files = [
        unlinked(1, "2026-05-17"),  # 45 days before the start: counted
        unlinked(2, "2026-05-16"),  # 46 days before: not
        unlinked(3, "2026-11-14"),  # 45 days after the end: counted
        unlinked(4, "2026-11-15"),  # 46 days after: not
        unlinked(5, "2026-08-01"),  # inside: counted
    ]

    assert match([], files, PERIOD).without_amount == 3


def test_a_receipt_outside_the_period_still_matches_inside_its_window():
    # A bank transaction on the period's first day accepts a receipt 30 days before it.
    result = match(
        targets([tx(1, on=date(2026, 7, 1))], {}),
        [unlinked(5, "2026-06-05", 875, "2026-06-01")],
        PERIOD,
    )

    assert [p.receipt.file_id for p in result.confident] == [5]
