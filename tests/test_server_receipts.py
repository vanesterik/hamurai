import httpx
import pytest
from fake_api import make_file, make_transaction
from test_server_read import connected, text

from hamurai.server import SESSION_EXPIRED

pytestmark = pytest.mark.anyio

PERIOD = {"start_date": "2026-07-01", "end_date": "2026-09-30"}
BANK = 1
SPLIT = 2


def split_booking(fake, transaction_id: int, amount: int, description: str) -> int:
    booking_id = fake.add_booking(transaction_id, 10, 20)
    fake.update_booking(
        booking_id,
        {
            "amount": amount,
            "generalLedgerAccountId": 10,
            "vatCodeId": 20,
            "description": description,
            "entryDate": "2026-08-17",
        },
    )
    return booking_id


@pytest.fixture
def path(tmp_path, fake):
    """A booked bank payment and a card collection split in two, each with a receipt."""
    fake.transactions += [
        make_transaction(BANK, iban=None, name="Shop", amount=-875, entry_date="2026-07-02"),
        make_transaction(SPLIT, iban=None, name="Card", amount=-1799, entry_date="2026-08-17"),
    ]
    fake.add_booking(BANK, 10, 20)
    split_booking(fake, SPLIT, -1299, "ACME SOFTWARE")
    split_booking(fake, SPLIT, -500, "SHOP")
    fake.files += [
        make_file(5, created_at="2026-07-01T10:00:00", amount_cents=875, receipt_date="2026-07-01"),
        make_file(
            6, created_at="2026-08-01T10:00:00", amount_cents=1299, receipt_date="2026-07-28"
        ),
        make_file(7, created_at="2026-08-02T10:00:00"),
    ]
    return tmp_path / "data" / "suggestions.yaml"


async def propose(session):
    return await session.call_tool("propose_receipts", PERIOD)


def writes(fake) -> list[httpx.Request]:
    return [r for r in fake.requests if r.method != "GET"]


async def test_propose_receipts_matches_bank_and_split(client, fake, path):
    before = len(writes(fake))
    async with connected(client, path) as session:
        result = await propose(session)

    assert not result.is_error, text(result)
    data = result.structured_content
    pairs = sorted(
        (p["receipt"]["file_id"], p["target"]["transaction_id"]) for p in data["confident"]
    )
    assert pairs == [(5, BANK), (6, SPLIT)]
    line = next(p["target"] for p in data["confident"] if p["receipt"]["file_id"] == 6)
    assert (line["counterparty"], line["split_line"]) == ("ACME SOFTWARE", True)
    assert data["without_amount"] == 1
    assert len(writes(fake)) == before


async def test_propose_receipts_reads_files_back_45_days(client, fake, path):
    # 120 files after the cutoff (2026-05-17), then 200 before it: two pages are enough.
    fake.files += [make_file(100 + i, created_at="2026-06-01T10:00:00") for i in range(120)]
    fake.files += [make_file(300 + i, created_at="2026-04-01T10:00:00") for i in range(200)]
    async with connected(client, path) as session:
        await propose(session)

    offsets = [
        r.url.params.get("offset", "0") for r in fake.requests if r.url.path.endswith("/files")
    ]
    assert offsets == ["0", "100"]


async def test_propose_receipts_never_shows_a_secret(client, fake, path):
    async with connected(client, path) as session:
        result = await propose(session)

    assert "SECRET-FILE" not in text(result) + str(result.structured_content)


async def test_propose_receipts_session_expired(client, fake, path):
    fake.expired = True
    async with connected(client, path) as session:
        result = await propose(session)

    assert result.is_error and SESSION_EXPIRED in text(result)
