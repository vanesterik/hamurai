import httpx
import pytest
from conftest import COOKIE, connected, text
from fake_api import make_transaction
from test_server_read import IBAN, OTHER_IBAN

from hamurai.server import SESSION_EXPIRED
from hamurai.suggestions import Entry, LearnedBooking, Pair, Suggestions, load, save

pytestmark = pytest.mark.anyio

PERIOD = {"start_date": "2026-07-01", "end_date": "2026-09-30"}


@pytest.fixture
def path(tmp_path, fake):
    """Suggestions: Acme was booked once to 4500. Three unbooked July transactions."""
    path = tmp_path / "data" / "suggestions.yaml"
    learned = LearnedBooking(id=1, category_id=10, vat_code_id=20, date="2026-04-01")
    save(Suggestions(entries=[Entry(iban=IBAN, name="Acme", bookings=[learned])]), path)
    fake.transactions += [
        make_transaction(10, iban=IBAN, name="Acme", entry_date="2026-07-02"),
        make_transaction(11, iban=OTHER_IBAN, name="Other", entry_date="2026-07-03"),
        make_transaction(12, iban=None, name="Third", entry_date="2026-08-04"),
    ]
    return path


def learned_for(path, iban: str) -> Entry:
    entry = load(path).by_iban(iban)
    assert entry is not None
    return entry


def line(transaction_id: int, category_id: int = 10, vat_code_id: int = 20) -> dict:
    return {
        "transaction_id": transaction_id,
        "category_id": category_id,
        "vat_code_id": vat_code_id,
    }


async def book(session, *lines: dict):
    proposed = await session.call_tool("propose_bookings", PERIOD)
    assert not proposed.is_error, text(proposed)
    return await session.call_tool("book", {"bookings": list(lines)})


def posts(fake) -> list[httpx.Request]:
    return [r for r in fake.requests if r.method == "POST"]


def statuses(result) -> list[tuple[int, str]]:
    return [(r["transaction_id"], r["status"]) for r in result.structured_content["lines"]]


async def test_book_books_and_learns(client, fake, path):
    async with connected(client, path) as session:
        result = await book(session, line(10), line(11, 11))

    assert statuses(result) == [(10, "booked"), (11, "booked")]
    booking_ids = [r["booking_id"] for r in result.structured_content["lines"]]
    assert sorted(fake.bookings) == sorted(booking_ids)
    assert learned_for(path, IBAN).counts == {Pair(category_id=10, vat_code_id=20): 2}
    assert learned_for(path, OTHER_IBAN).current == Pair(category_id=11, vat_code_id=20)


async def test_book_refuses_duplicate_lines(client, fake, path):
    async with connected(client, path) as session:
        result = await book(session, line(10), line(10, 11))

    assert result.is_error
    assert "10" in text(result)
    assert posts(fake) == []


async def test_book_refuses_unproposed_transaction(client, fake, path):
    async with connected(client, path) as session:
        result = await session.call_tool("book", {"bookings": [line(10)]})

    assert result.is_error
    assert "propose_bookings" in text(result)
    assert posts(fake) == []


async def test_book_refuses_empty_list(client, fake, path):
    async with connected(client, path) as session:
        result = await book(session)

    assert result.is_error
    assert "No lines" in text(result)


@pytest.mark.parametrize(("category_id", "vat_code_id"), [(999, 20), (10, 999)])
async def test_book_refuses_unknown_category_or_vat(client, fake, path, category_id, vat_code_id):
    async with connected(client, path) as session:
        result = await book(session, line(11), line(10, category_id, vat_code_id))

    assert result.is_error
    assert "999" in text(result)
    assert posts(fake) == []


async def test_book_skips_already_booked(client, fake, path):
    async with connected(client, path) as session:
        await session.call_tool("propose_bookings", PERIOD)
        fake.add_booking(10, 11, 20)  # booked in the web app in the meantime
        result = await session.call_tool("book", {"bookings": [line(10), line(11)]})

    assert statuses(result) == [(10, "failed"), (11, "booked")]
    assert result.structured_content["lines"][0]["reason"] == "already booked"
    assert [r.content for r in posts(fake)][0].count(b'"transactionId":11') == 1
    assert len(posts(fake)) == 1


async def test_one_failing_line_does_not_stop_others(client, fake, path):
    fake.fail_posts = [422]

    async with connected(client, path) as session:
        result = await book(session, line(10), line(11))

    assert statuses(result) == [(10, "failed"), (11, "booked")]
    assert result.structured_content["lines"][0]["reason"] == "Fake error 422"
    assert len(learned_for(path, IBAN).bookings) == 1


async def test_timeout_is_unknown_and_not_learned(client, fake, path):
    fake.fail_posts = [httpx.ReadTimeout("slow")]

    async with connected(client, path) as session:
        result = await book(session, line(10))

    assert statuses(result) == [(10, "unknown")]
    assert "propose_bookings" in result.structured_content["lines"][0]["reason"]
    assert len(learned_for(path, IBAN).bookings) == 1


async def test_session_expired_stops_the_batch(client, fake, path):
    fake.expire_after_posts = 1

    async with connected(client, path) as session:
        result = await book(session, line(10), line(11), line(12))

    assert not result.is_error
    assert statuses(result) == [(10, "booked"), (11, "not_tried"), (12, "not_tried")]
    assert result.structured_content["session_expired"] is True
    assert result.structured_content["message"] == SESSION_EXPIRED
    assert len(posts(fake)) == 2


async def test_unlearnable_booking_is_still_booked(client, fake, path):
    fake.transactions.append(make_transaction(13, iban=None, name=None, entry_date="2026-07-05"))

    async with connected(client, path) as session:
        result = await book(session, line(13))

    assert statuses(result) == [(13, "booked")]
    assert len(load(path).entries) == 1


async def test_undo_deletes_and_takes_back_count(client, fake, path):
    async with connected(client, path) as session:
        booked = await book(session, line(10, 11))
        booking_id = booked.structured_content["lines"][0]["booking_id"]
        assert learned_for(path, IBAN).current == Pair(category_id=11, vat_code_id=20)

        result = await session.call_tool("undo_booking", {"booking_id": booking_id})

    assert result.structured_content == {
        "booking_id": booking_id,
        "forgotten": True,
        "already_gone": False,
    }
    assert fake.bookings == {}
    entry = learned_for(path, IBAN)
    assert entry.current == Pair(category_id=10, vat_code_id=20)
    assert len(entry.bookings) == 1


async def test_undo_after_reconnect_uses_the_file(client, fake, path):
    async with connected(client, path) as session:
        booked = await book(session, line(10))
    booking_id = booked.structured_content["lines"][0]["booking_id"]

    async with connected(client, path) as session:
        result = await session.call_tool("undo_booking", {"booking_id": booking_id})

    assert result.structured_content["forgotten"] is True
    assert len(learned_for(path, IBAN).bookings) == 1


async def test_undo_of_unknown_booking_is_an_error(client, fake, path):
    async with connected(client, path) as session:
        result = await session.call_tool("undo_booking", {"booking_id": 424242})

    assert result.is_error
    assert "No booking 424242, in Hamurai or learned." in text(result)
    assert "Hamurai answered" not in text(result)


async def test_undo_keeps_suggestions_when_delete_fails(client, fake, path):
    async with connected(client, path) as session:
        booked = await book(session, line(10))
        booking_id = booked.structured_content["lines"][0]["booking_id"]
        fake.fail_next = [500]
        result = await session.call_tool("undo_booking", {"booking_id": booking_id})

    assert result.is_error
    assert len(learned_for(path, IBAN).bookings) == 2


@pytest.mark.parametrize("failure", [None, "expired", 500, "timeout"])
async def test_cookie_never_in_output(client, fake, path, failure):
    secret = COOKIE.split("=", 1)[1]

    async with connected(client, path) as session:
        await session.call_tool("propose_bookings", PERIOD)
        if failure == "expired":
            fake.expired = True
        elif failure == 500:
            fake.fail_posts = [500]
            fake.fail_next = []
        elif failure == "timeout":
            fake.fail_posts = [httpx.ReadTimeout("slow")]
        results = [
            await session.call_tool("book", {"bookings": [line(10)]}),
            await session.call_tool("undo_booking", {"booking_id": 9001}),
        ]

    for result in results:
        assert secret not in text(result)
        assert secret not in str(result.structured_content)


async def test_failing_to_learn_still_reports_the_booking(client, fake, path, monkeypatch):
    def broken_save(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("hamurai.server.save", broken_save)

    async with connected(client, path) as session:
        result = await book(session, line(10), line(11))

    assert statuses(result) == [(10, "booked"), (11, "booked")]
    assert "not learned" in result.structured_content["lines"][0]["reason"]
    assert len(fake.bookings) == 2


async def test_unreadable_booking_answer_is_unknown(client, fake, path):
    fake.fail_posts = [httpx.Response(201, text="not json")]

    async with connected(client, path) as session:
        result = await book(session, line(10), line(11))

    assert statuses(result) == [(10, "unknown"), (11, "booked")]


async def test_undo_of_booking_already_gone_still_forgets(client, fake, path):
    async with connected(client, path) as session:
        booked = await book(session, line(10, 11))
        booking_id = booked.structured_content["lines"][0]["booking_id"]
        del fake.bookings[booking_id]  # deleted in the web app in the meantime

        result = await session.call_tool("undo_booking", {"booking_id": booking_id})

    assert not result.is_error, text(result)
    assert result.structured_content["forgotten"] is True
    assert result.structured_content["already_gone"] is True
    assert learned_for(path, IBAN).current == Pair(category_id=10, vat_code_id=20)
