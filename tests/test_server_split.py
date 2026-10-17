import pytest
from fake_api import make_transaction
from test_server_read import connected, text
from test_statement import ACME, CLOUD, PAYMENT, write_statement

from hamurai.suggestions import Entry, LearnedBooking, Suggestions, save

pytestmark = pytest.mark.anyio

COLLECTION = 50


@pytest.fixture
def path(tmp_path, fake):
    """Suggestions: ACME SOFTWARE was booked once to 4500. One unbooked card collection."""
    path = tmp_path / "data" / "suggestions.yaml"
    learned = LearnedBooking(id=1, category_id=10, vat_code_id=20, date="2026-04-01")
    save(Suggestions(entries=[Entry(name="ACME SOFTWARE", bookings=[learned])]), path)
    fake.transactions.append(
        make_transaction(
            COLLECTION, iban=None, name="Card Company", amount=-9761, entry_date="2026-09-21"
        )
    )
    return path


@pytest.fixture
def statement(tmp_path):
    return write_statement(tmp_path, [PAYMENT, ACME, CLOUD])


async def propose(session, statement):
    return await session.call_tool("propose_split", {"statement_path": str(statement)})


async def test_propose_split_finds_the_collection_and_proposes(client, fake, path, statement):
    async with connected(client, path) as session:
        result = await propose(session, statement)

    assert not result.is_error, text(result)
    data = result.structured_content
    assert data["collection"]["id"] == COLLECTION
    assert data["total_cents"] == -9761
    assert [(p["line"]["number"], p["confidence"]) for p in data["lines"]] == [
        (1, "confident"),
        (2, "unknown"),
    ]
    assert (data["lines"][0]["category_id"], data["lines"][0]["vat_code_id"]) == (10, 20)
    assert [o["reason"] for o in data["left_out"]] == ["card payment"]
    assert [r for r in fake.requests if r.method != "GET"] == []


async def test_propose_split_looks_45_days_past_the_last_booking_date(
    client, fake, path, statement
):
    async with connected(client, path) as session:
        await propose(session, statement)

    [listing] = [r for r in fake.requests if r.url.path.endswith("/transactions")]
    assert listing.url.params["startDate"] == "2026-08-25"
    assert listing.url.params["endDate"] == "2026-10-15"


async def test_propose_split_without_a_matching_collection(client, fake, path, statement):
    fake.transactions[0]["amount"] = -9760
    async with connected(client, path) as session:
        result = await propose(session, statement)

    assert result.is_error
    assert "-9761" in text(result)
    assert "nothing is booked" in text(result)


async def test_propose_split_skips_a_booked_collection(client, fake, path, statement):
    fake.add_booking(COLLECTION, 10, 20)
    async with connected(client, path) as session:
        result = await propose(session, statement)

    assert result.is_error
    assert "No unbooked transaction" in text(result)


async def test_propose_split_with_two_candidates(client, fake, path, statement):
    fake.transactions.append(
        make_transaction(51, iban=None, name="Other", amount=-9761, entry_date="2026-09-30")
    )
    async with connected(client, path) as session:
        result = await propose(session, statement)

    assert result.is_error
    assert "2026-09-21" in text(result) and "2026-09-30" in text(result)


@pytest.mark.parametrize(
    "spelling",
    [
        lambda p: str(p),
        lambda p: str(p).replace(" ", "\\ "),
        lambda p: f"'{p}'",
        lambda p: f' "{p}" ',
    ],
)
async def test_propose_split_accepts_a_dragged_path(client, path, tmp_path, spelling):
    statement = write_statement(tmp_path, [PAYMENT, ACME, CLOUD], name="My Statements/card.csv")
    async with connected(client, path) as session:
        result = await session.call_tool("propose_split", {"statement_path": spelling(statement)})

    assert not result.is_error, text(result)


async def test_propose_split_accepts_a_home_path(client, path, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    write_statement(tmp_path, [PAYMENT, ACME, CLOUD], name="Desktop/card.csv")
    async with connected(client, path) as session:
        result = await session.call_tool("propose_split", {"statement_path": "~/Desktop/card.csv"})

    assert not result.is_error, text(result)


async def test_propose_split_never_shows_the_card_number(client, path, tmp_path, statement):
    bad = write_statement(tmp_path, [ACME, ("2026-08-24", "2026-08-25", "X", "abc")], name="b.csv")
    async with connected(client, path) as session:
        results = [await propose(session, statement), await propose(session, bad)]

    assert results[1].is_error
    for result in results:
        shown = text(result) + str(result.structured_content)
        for value in ("5555", "00000000001", "JANSEN"):
            assert value not in shown
