import re
from datetime import date

import anyio
import httpx
import pytest
from conftest import connected, text
from fake_api import make_transaction
from test_statement import ACME, CLOUD, PAYMENT, write_statement

from hamurai.server import SESSION_EXPIRED
from hamurai.suggestions import Entry, LearnedBooking, Suggestions, load, save

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
        lambda p: re.sub(r"([ '()])", r"\\\1", str(p)),
        lambda p: f"'{p}'",
        lambda p: f' "{p}" ',
    ],
)
async def test_propose_split_accepts_a_dragged_path(client, path, tmp_path, spelling):
    statement = write_statement(
        tmp_path, [PAYMENT, ACME, CLOUD], name="My Statements/J's card (1).csv"
    )
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


def split_line(number: int, category_id: int = 10, vat_code_id: int = 20) -> dict:
    return {"line": number, "category_id": category_id, "vat_code_id": vat_code_id}


BOTH = [split_line(1, 10, 20), split_line(2, 11, 21)]


async def book_split(session, statement, lines=BOTH, transaction_id=COLLECTION):
    proposed = await propose(session, statement)
    assert not proposed.is_error, text(proposed)
    return await session.call_tool("book_split", {"transaction_id": transaction_id, "lines": lines})


def writes(fake) -> list:
    return [r for r in fake.requests if r.method != "GET"]


def bookings_on_collection(fake) -> list[dict]:
    return [b for b in fake.bookings.values() if b["transactionInfo"]["id"] == COLLECTION]


async def test_book_split_books_every_line_and_learns(client, fake, path, statement):
    async with connected(client, path) as session:
        result = await book_split(session, statement)

    assert not result.is_error, text(result)
    data = result.structured_content
    assert data["status"] == "booked"
    assert [b["line"] for b in data["bookings"]] == [1, 2]
    booked = bookings_on_collection(fake)
    assert [
        (b["amountInCents"], b["description"], b["entryDate"], b["vatCode"]["id"]) for b in booked
    ] == [(-1299, "ACME SOFTWARE", "2026-09-21", 20), (-8462, "EXAMPLE CLOUD", "2026-09-21", 21)]
    assert fake.transactions[0]["isFullyReconciled"]
    learned = load(path)
    acme = learned.by_name("ACME SOFTWARE")
    cloud = learned.by_name("EXAMPLE CLOUD")
    assert acme is not None and cloud is not None
    assert [b.date for b in acme.bookings] == [date(2026, 4, 1), date(2026, 8, 30)]
    assert [(b.category_id, b.vat_code_id, b.date) for b in cloud.bookings] == [
        (11, 21, date(2026, 8, 24))
    ]


@pytest.mark.parametrize("lines", [[split_line(1), split_line(1)], [split_line(1)]])
async def test_book_split_refuses_a_missing_or_repeated_line(client, fake, path, statement, lines):
    async with connected(client, path) as session:
        result = await book_split(session, statement, lines)

    assert result.is_error
    assert "line" in text(result)
    assert writes(fake) == []


async def test_book_split_refuses_without_a_proposal(client, fake, path):
    async with connected(client, path) as session:
        result = await session.call_tool(
            "book_split", {"transaction_id": COLLECTION, "lines": BOTH}
        )

    assert result.is_error
    assert "propose_split" in text(result)
    assert writes(fake) == []


async def test_book_split_refuses_another_transaction(client, fake, path, statement):
    async with connected(client, path) as session:
        result = await book_split(session, statement, transaction_id=51)

    assert result.is_error
    assert "propose_split" in text(result)
    assert writes(fake) == []


async def test_book_split_refuses_unknown_ids(client, fake, path, statement):
    async with connected(client, path) as session:
        result = await book_split(session, statement, [split_line(1), split_line(2, 99)])

    assert result.is_error
    assert "99" in text(result)
    assert writes(fake) == []


async def test_book_split_refuses_a_collection_booked_since(client, fake, path, statement):
    async with connected(client, path) as session:
        proposed = await propose(session, statement)
        assert not proposed.is_error
        fake.add_booking(COLLECTION, 10, 20)
        before = len(writes(fake))
        result = await session.call_tool(
            "book_split", {"transaction_id": COLLECTION, "lines": BOTH}
        )

    assert result.is_error
    assert "already booked" in text(result)
    assert len(writes(fake)) == before


async def test_failed_update_rolls_back(client, fake, path, statement):
    fake.fail_updates = [500]
    async with connected(client, path) as session:
        result = await book_split(session, statement)

    data = result.structured_content
    assert data["status"] == "rolled_back"
    assert data["left_behind"] == []
    assert bookings_on_collection(fake) == []
    assert fake.transactions[0]["numberOfReconciliations"] == 0
    assert load(path).by_name("EXAMPLE CLOUD") is None


async def test_failure_on_a_later_line_rolls_back_the_earlier_ones(client, fake, path, statement):
    fake.fail_creates = [None, 500]
    async with connected(client, path) as session:
        result = await book_split(session, statement)

    assert result.structured_content["status"] == "rolled_back"
    assert bookings_on_collection(fake) == []
    acme = load(path).by_name("ACME SOFTWARE")
    assert acme is not None
    assert [b.id for b in acme.bookings] == [1]


async def test_undeletable_booking_is_left_behind(client, fake, path, statement):
    fake.fail_updates = [500]
    fake.fail_deletes = [500]
    async with connected(client, path) as session:
        result = await book_split(session, statement)

    data = result.structured_content
    assert data["status"] == "incomplete"
    [left] = bookings_on_collection(fake)
    assert data["left_behind"] == [left["id"]]
    assert "undo_booking" in data["message"]


async def test_unknown_create_outcome_is_incomplete(client, fake, path, statement):
    fake.fail_creates = [None, httpx.ReadTimeout("timeout")]
    async with connected(client, path) as session:
        result = await book_split(session, statement)

    # Nothing was made, and looking at the collection afterwards shows it.
    data = result.structured_content
    assert data["status"] == "rolled_back"
    assert "nothing is booked" in data["message"]
    assert bookings_on_collection(fake) == []


async def test_unknown_create_outcome_unchecked_is_incomplete(client, fake, path, statement):
    fake.fail_creates = [None, httpx.ReadTimeout("timeout")]
    fake.fail_routes["/reconciliations"] = [httpx.ConnectError("down")] * 2
    async with connected(client, path) as session:
        result = await book_split(session, statement)

    data = result.structured_content
    assert data["status"] == "incomplete"
    assert "check the collection" in data["message"]


async def test_session_expiry_mid_split(client, fake, path, statement):
    fake.expire_after_posts = 1
    async with connected(client, path) as session:
        result = await book_split(session, statement)

    data = result.structured_content
    assert data["session_expired"]
    assert data["status"] == "incomplete"
    assert data["message"].startswith(SESSION_EXPIRED)
    [left] = bookings_on_collection(fake)
    assert data["left_behind"] == [left["id"]]


async def test_book_split_twice_is_refused(client, fake, path, statement):
    async with connected(client, path) as session:
        first = await book_split(session, statement)
        assert first.structured_content["status"] == "booked"
        before = len(writes(fake))
        again = await session.call_tool("book_split", {"transaction_id": COLLECTION, "lines": BOTH})

    assert again.is_error
    assert len(writes(fake)) == before


async def test_unknown_update_outcome_still_rolls_back(client, fake, path, statement):
    fake.fail_updates = [httpx.ReadTimeout("timeout")]
    async with connected(client, path) as session:
        result = await book_split(session, statement)

    data = result.structured_content
    assert data["status"] == "rolled_back"
    assert "nothing is booked" in data["message"]
    assert bookings_on_collection(fake) == []


async def test_create_applied_but_answered_504_is_reported(client, fake, path, statement):
    fake.fail_creates = [None, ("after", 504)]
    async with connected(client, path) as session:
        result = await book_split(session, statement)

    data = result.structured_content
    assert data["status"] == "incomplete"
    [left] = bookings_on_collection(fake)
    assert data["left_behind"] == [left["id"]]
    assert "nothing is booked" not in data["message"]


async def test_update_applied_but_answer_lost_still_rolls_back(client, fake, path, statement):
    fake.fail_updates = [("after", httpx.ReadTimeout("timeout"))]
    async with connected(client, path) as session:
        result = await book_split(session, statement)

    assert result.structured_content["status"] == "rolled_back"
    assert bookings_on_collection(fake) == []


async def test_deleted_meanwhile_counts_as_deleted(client, fake, path, statement):
    fake.fail_updates = [500]
    fake.fail_deletes = [("after", 404)]
    async with connected(client, path) as session:
        result = await book_split(session, statement)

    data = result.structured_content
    assert (data["status"], data["left_behind"]) == ("rolled_back", [])


async def test_cancelled_split_rolls_back(client, fake, path, statement):
    async with connected(client, path) as session:
        proposed = await propose(session, statement)
        assert not proposed.is_error
        # Each request takes 50 ms; the call is cancelled after the first create.
        fake.delay = 0.05
        with anyio.move_on_after(0.12):
            await session.call_tool("book_split", {"transaction_id": COLLECTION, "lines": BOTH})
        made = [r for r in fake.requests if r.method == "POST"]
        await anyio.sleep(0.5)

    assert made, "the call was cancelled before it booked anything"
    assert bookings_on_collection(fake) == []
    assert any(r.method == "DELETE" for r in fake.requests)


async def test_parallel_book_splits_book_once(client, fake, path, statement):
    results = []
    async with connected(client, path) as session:
        proposed = await propose(session, statement)
        assert not proposed.is_error
        fake.delay = 0.01

        async def call() -> None:
            results.append(
                await session.call_tool("book_split", {"transaction_id": COLLECTION, "lines": BOTH})
            )

        async with anyio.create_task_group() as group:
            group.start_soon(call)
            group.start_soon(call)

    assert sorted(r.is_error for r in results) == [False, True]
    assert [b["amountInCents"] for b in bookings_on_collection(fake)] == [-1299, -8462]


async def test_a_changed_remainder_stops_the_split(client, fake, path, statement):
    async with connected(client, path) as session:
        proposed = await propose(session, statement)
        assert not proposed.is_error
        # Booked in the web app after the check, before the first create: the create
        # then gets a remainder of 0.
        fake.fail_creates = [None]
        original = fake.add_booking

        def booked_meanwhile(transaction_id, category_id, vat_code_id):
            fake.add_booking = original
            original(transaction_id, category_id, vat_code_id)
            return original(transaction_id, category_id, vat_code_id)

        fake.add_booking = booked_meanwhile
        result = await session.call_tool(
            "book_split", {"transaction_id": COLLECTION, "lines": BOTH}
        )

    data = result.structured_content
    assert data["status"] == "incomplete"
    [left] = bookings_on_collection(fake)
    assert data["left_behind"] == [left["id"]]


async def test_book_split_books_a_credit_last(client, fake, path, tmp_path):
    credit = ("2026-08-20", "2026-08-21", "SHOP A", "7,50")
    statement = write_statement(tmp_path, [credit, ACME, CLOUD], name="credit.csv")
    fake.transactions[0]["amount"] = -9011
    lines = [split_line(1, 10, 20), split_line(2, 10, 20), split_line(3, 11, 21)]
    async with connected(client, path) as session:
        result = await book_split(session, statement, lines)

    assert not result.is_error, text(result)
    assert result.structured_content["status"] == "booked"
    assert [b["amountInCents"] for b in bookings_on_collection(fake)] == [-1299, -8462, 750]
    assert fake.transactions[0]["isFullyReconciled"]


async def test_an_update_that_keeps_another_amount_rolls_back(client, fake, path, statement):
    fake.ignore_update_amounts = True
    async with connected(client, path) as session:
        result = await book_split(session, statement)

    data = result.structured_content
    assert data["status"] == "rolled_back"
    assert "amount" in data["message"]
    assert bookings_on_collection(fake) == []
    assert load(path).by_name("EXAMPLE CLOUD") is None
