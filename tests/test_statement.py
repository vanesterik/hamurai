from datetime import date
from pathlib import Path

import pytest

from hamurai.statement import StatementError, parse_amount, read_statement

HEADER = (
    "Accountnummer",
    "Kaartnummer",
    "Naam op kaart",
    "Transactiedatum",
    "Boekingsdatum",
    "Omschrijving",
    "Valuta",
    "Bedrag",
    "Koers",
    "Bedrag in EUR",
)
PAYMENT = ("2026-08-17", "2026-08-17", "BETALING ONTVANGEN - DANK U", "293,50")
ACME = ("2026-08-30", "2026-08-31", "ACME SOFTWARE", "-12,99")
CLOUD = ("2026-08-24", "2026-08-25", "EXAMPLE CLOUD", "-84,62")


def write_statement(
    tmp_path: Path,
    rows: list[tuple[str, str, str, str]],
    blank_after_first: bool = True,
    header: tuple[str, ...] = HEADER,
    name: str = "statement.csv",
) -> Path:
    """A statement in the real file's format, with invented values in the other columns."""
    lines = [",".join(f'"{h}"' for h in header)]
    for n, (transaction_date, booking_date, description, amount) in enumerate(rows):
        values = {
            "Accountnummer": "'00000000001",
            "Kaartnummer": "5555 99** **** 0001",
            "Naam op kaart": "J JANSEN",
            "Transactiedatum": transaction_date,
            "Boekingsdatum": booking_date,
            "Omschrijving": description,
            "Valuta": "EUR",
            "Bedrag": amount,
            "Koers": "",
            "Bedrag in EUR": amount,
        }
        lines.append(",".join(f'"{values[h]}"' for h in header))
        if n == 0 and blank_after_first:
            lines.append("")
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode())
    return path


def test_parse_amount():
    assert parse_amount("-12,99") == -1299
    assert parse_amount("-1.234,56") == -123456
    assert parse_amount("293,50") == 29350
    assert parse_amount("-5,00") == -500
    with pytest.raises(ValueError):
        parse_amount("12.99")
    with pytest.raises(ValueError):
        parse_amount("")


def test_reads_lines_and_leaves_out_the_payment(tmp_path):
    statement = read_statement(write_statement(tmp_path, [PAYMENT, ACME, CLOUD]))
    assert [(line.number, line.description, line.amount_cents) for line in statement.lines] == [
        (1, "ACME SOFTWARE", -1299),
        (2, "EXAMPLE CLOUD", -8462),
    ]
    assert statement.lines[0].transaction_date == date(2026, 8, 30)
    assert statement.lines[0].booking_date == date(2026, 8, 31)
    assert [(o.reason, o.amount_cents) for o in statement.left_out] == [("card payment", 29350)]
    assert statement.total_cents == -9761
    assert statement.first_booking_date == date(2026, 8, 25)
    assert statement.last_booking_date == date(2026, 8, 31)


def test_identifying_columns_are_not_kept(tmp_path):
    statement = read_statement(write_statement(tmp_path, [PAYMENT, ACME, CLOUD]))
    dumped = statement.model_dump_json()
    for value in ("5555", "00000000001", "JANSEN"):
        assert value not in dumped


def test_refund_is_left_out_with_its_purchase(tmp_path):
    rows = [
        ("2026-08-01", "2026-08-02", "SHOP A", "-20,00"),
        ("2026-08-03", "2026-08-04", "SHOP B", "-5,00"),
        ("2026-08-05", "2026-08-06", "SHOP A", "20,00"),
    ]
    statement = read_statement(write_statement(tmp_path, rows))
    assert [(line.number, line.description) for line in statement.lines] == [(1, "SHOP B")]
    assert [(o.reason, o.description, o.amount_cents) for o in statement.left_out] == [
        ("refunded", "SHOP A", -2000),
        ("refund", "SHOP A", 2000),
    ]


def test_refund_prefers_the_purchase_with_its_description(tmp_path):
    rows = [
        ("2026-08-01", "2026-08-02", "SHOP A", "-20,00"),
        ("2026-08-03", "2026-08-04", "SHOP B", "-20,00"),
        ("2026-08-05", "2026-08-06", "Shop  B", "20,00"),
    ]
    statement = read_statement(write_statement(tmp_path, rows))
    assert [line.description for line in statement.lines] == ["SHOP A"]


def test_refund_pairs_within_one_merchant(tmp_path):
    rows = [
        ("2026-08-01", "2026-08-02", "SHOP A", "-20,00"),
        ("2026-08-03", "2026-08-04", "SHOP A", "-20,00"),
        ("2026-08-05", "2026-08-06", "SHOP A REFUND", "20,00"),
    ]
    statement = read_statement(write_statement(tmp_path, rows))
    assert [line.description for line in statement.lines] == ["SHOP A"]


def test_unmatched_credit_is_a_line(tmp_path):
    rows = [ACME, ("2026-08-05", "2026-08-06", "SHOP A", "7,50")]
    statement = read_statement(write_statement(tmp_path, rows))
    assert [(line.number, line.description, line.amount_cents) for line in statement.lines] == [
        (1, "ACME SOFTWARE", -1299),
        (2, "SHOP A", 750),
    ]
    assert statement.total_cents == -549
    assert statement.left_out == []


def test_ambiguous_refund_is_refused(tmp_path):
    rows = [
        ("2026-08-01", "2026-08-02", "SHOP A", "-20,00"),
        ("2026-08-03", "2026-08-04", "SHOP B", "-20,00"),
        ("2026-08-05", "2026-08-06", "SHOP C", "20,00"),
    ]
    with pytest.raises(StatementError, match="more than one merchant"):
        read_statement(write_statement(tmp_path, rows))


def test_missing_column_is_named(tmp_path):
    header = HEADER[:-1]
    with pytest.raises(StatementError, match="Bedrag in EUR"):
        read_statement(write_statement(tmp_path, [ACME], header=header))


def test_bad_amount_names_the_row_not_the_value(tmp_path):
    rows = [ACME, ("2026-08-24", "2026-08-25", "EXAMPLE CLOUD", "abc")]
    with pytest.raises(StatementError, match="row 2") as error:
        read_statement(write_statement(tmp_path, rows))
    assert "abc" not in str(error.value)


def test_bad_date_names_the_row(tmp_path):
    rows = [("31-08-2026", "2026-08-31", "ACME SOFTWARE", "-12,99")]
    with pytest.raises(StatementError, match="row 1"):
        read_statement(write_statement(tmp_path, rows))


def test_statement_without_purchases_is_refused(tmp_path):
    with pytest.raises(StatementError, match="no purchases"):
        read_statement(write_statement(tmp_path, [PAYMENT]))
    with pytest.raises(StatementError, match="no purchases"):
        read_statement(write_statement(tmp_path, [], name="empty.csv"))


def test_unreadable_file_is_refused(tmp_path):
    with pytest.raises(StatementError, match="missing.csv"):
        read_statement(tmp_path / "missing.csv")
    path = tmp_path / "latin.csv"
    path.write_bytes(b'"Omschrijving"\r\n"caf\xe9"\r\n')
    with pytest.raises(StatementError, match="UTF-8"):
        read_statement(path)


def test_zero_row_is_left_out(tmp_path):
    rows = [ACME, ("2026-08-05", "2026-08-06", "WAIVED FEE", "0,00"), CLOUD]
    statement = read_statement(write_statement(tmp_path, rows))

    assert [line.description for line in statement.lines] == ["ACME SOFTWARE", "EXAMPLE CLOUD"]
    assert [(o.reason, o.description, o.amount_cents) for o in statement.left_out] == [
        ("zero", "WAIVED FEE", 0)
    ]


def test_statement_with_only_a_zero_row_is_refused(tmp_path):
    rows = [("2026-08-05", "2026-08-06", "WAIVED FEE", "0,00")]
    with pytest.raises(StatementError, match="no purchases"):
        read_statement(write_statement(tmp_path, rows))


def test_unreadable_csv_is_refused(tmp_path):
    # Python's csv reads on past an unclosed quote, but refuses a field over its size limit.
    path = tmp_path / "huge.csv"
    path.write_text(",".join(HEADER) + "\r\n" + "x" * 200_000 + "\r\n")

    with pytest.raises(StatementError, match="can't be read as CSV") as error:
        read_statement(path)

    assert "xxx" not in str(error.value)
