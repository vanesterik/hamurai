import json
from datetime import date

import httpx
import pytest
from conftest import COOKIE
from fake_api import make_transaction

from hamurai.client import BookingOutcomeUnknown, HamuraiError, SessionExpired

pytestmark = pytest.mark.anyio


def add_transactions(fake, count: int) -> None:
    fake.transactions += [
        make_transaction(i, iban=f"NL00TEST{i:010d}", name=f"Counterparty {i}")
        for i in range(1, count + 1)
    ]


async def test_transactions_pages_until_last(client, fake):
    add_transactions(fake, 1200)

    transactions = await client.transactions()

    assert len(transactions) == 1200
    pages = [(r.url.params["page"], r.url.params["size"]) for r in fake.requests]
    assert pages == [("0", "500"), ("1", "500"), ("2", "500")]


async def test_transactions_passes_journal_and_dates(client, fake):
    await client.transactions(date(2026, 7, 1), date(2026, 9, 30))

    params = fake.requests[0].url.params
    assert params["journalId"] == "22222"
    assert params["startDate"] == "2026-07-01"
    assert params["endDate"] == "2026-09-30"


async def test_transaction_fields(client, fake):
    fake.transactions.append(
        make_transaction(
            7, iban="NL00TEST0000000007", name="Seven", description="Invoice 7", amount=-1000
        )
    )

    [transaction] = await client.transactions()

    assert transaction.amount_cents == -1000
    assert transaction.entry_date == date(2026, 7, 1)
    assert transaction.counter_account.number == "NL00TEST0000000007"
    assert transaction.counter_account.name_account_holder == "Seven"
    assert transaction.is_fully_reconciled is False


async def test_unauthorised_raises_session_expired(client, fake):
    fake.expired = True

    with pytest.raises(SessionExpired):
        await client.vat_codes()


async def test_create_booking_sends_exact_body(client, fake):
    fake.transactions.append(make_transaction(1, iban="NL00TEST0000000001", name="One"))

    booking = await client.create_booking(1, 10, 20)

    assert json.loads(fake.requests[-1].content) == {
        "type": "GENERAL_LEDGER_ACCOUNT",
        "transactionId": 1,
        "generalLedgerAccountId": 10,
        "vatCodeId": 20,
        "suggestion": None,
        "suggestionAccepted": False,
    }
    assert (booking.transaction_id, booking.category_id, booking.vat_code_id) == (1, 10, 20)
    assert booking.amount_cents == -1000


async def test_bookings_for_reads_ids(client, fake):
    fake.transactions.append(make_transaction(1, iban="NL00TEST0000000001", name="One"))
    booking_id = fake.add_booking(1, 11, 21)

    [booking] = await client.bookings_for(1)

    assert (booking.id, booking.category_id, booking.vat_code_id) == (booking_id, 11, 21)
    assert fake.requests[-1].url.params["transactionId"] == "1"


async def test_delete_booking_uses_booking_id(client, fake):
    fake.transactions.append(make_transaction(1, iban="NL00TEST0000000001", name="One"))
    booking_id = fake.add_booking(1, 10, 20)

    await client.delete_booking(booking_id)

    assert fake.requests[-1].method == "DELETE"
    assert fake.requests[-1].url.path.endswith(f"/reconciliations/{booking_id}")
    assert fake.bookings == {}


async def test_reads_are_retried_once(client, fake):
    fake.fail_next = [httpx.ConnectError("down")]

    assert await client.transactions() == []
    assert len(fake.requests) == 2


async def test_reads_fail_after_second_network_error(client, fake):
    fake.fail_next = [httpx.ConnectError("down"), httpx.ConnectError("down")]

    with pytest.raises(httpx.TransportError):
        await client.transactions()


async def test_create_booking_is_never_retried(client, fake):
    fake.transactions.append(make_transaction(1, iban="NL00TEST0000000001", name="One"))
    fake.fail_next = [httpx.ReadTimeout("slow")]

    with pytest.raises(BookingOutcomeUnknown):
        await client.create_booking(1, 10, 20)

    assert [r.method for r in fake.requests] == ["POST"]


async def test_error_message_comes_from_hamurai(client, fake):
    fake.fail_next = [422]

    with pytest.raises(HamuraiError) as err:
        await client.create_booking(1, 10, 20)

    assert err.value.status == 422
    assert err.value.message == "Fake error 422"


async def test_bookings_for_many_limits_concurrency(client, fake):
    add_transactions(fake, 20)
    for i in range(1, 21):
        fake.add_booking(i, 10, 20)
    fake.delay = 0.01
    progress = []

    async def on_progress(done: int, total: int) -> None:
        progress.append((done, total))

    found = await client.bookings_for_many(
        list(range(1, 21)), concurrency=4, on_progress=on_progress
    )

    assert sorted(found) == list(range(1, 21))
    assert all(len(bookings) == 1 for bookings in found.values())
    assert fake.max_in_flight == 4
    assert len(progress) == 20
    assert progress[-1] == (20, 20)


async def test_cookie_header_is_sent_verbatim(client, fake):
    await client.vat_codes()

    assert fake.requests[0].headers["Cookie"] == COOKIE


async def test_lists(client):
    categories = await client.categories()
    vat_codes = await client.vat_codes()

    assert [(c.id, c.code, c.name) for c in categories][0] == (10, "4500", "Kantoorkosten")
    assert [(v.id, v.description) for v in vat_codes][0] == (20, "21% hoog")


async def test_create_booking_takes_transaction_id_from_the_request(client, fake):
    fake.transactions.append(make_transaction(1, iban="NL00TEST0000000001", name="One"))

    booking = await client.create_booking(1, 10, 20)

    assert booking.transaction_id == 1


async def test_unreadable_booking_answer_is_unknown(client, fake):
    fake.transactions.append(make_transaction(1, iban="NL00TEST0000000001", name="One"))
    fake.fail_posts = [httpx.Response(201, text="not json")]

    with pytest.raises(BookingOutcomeUnknown):
        await client.create_booking(1, 10, 20)


async def test_bookings_for_many_raises_session_expired_unwrapped(client, fake):
    add_transactions(fake, 3)
    fake.fail_routes = {"/reconciliations": [401]}

    with pytest.raises(SessionExpired):
        await client.bookings_for_many([1, 2, 3])


async def test_bookings_for_skips_bookings_not_onto_a_category(client, fake):
    fake.transactions.append(make_transaction(1, iban="NL00TEST0000000001", name="One"))
    fake.add_other_booking(1)

    assert await client.bookings_for(1) == []


async def test_null_description_and_counter_account(client, fake):
    transaction = make_transaction(1, iban=None, name=None)
    transaction["description"] = None
    transaction["counterAccount"] = None
    fake.transactions.append(transaction)

    [parsed] = await client.transactions()

    assert parsed.description == ""
    assert parsed.counter_account.number is None


@pytest.mark.parametrize("call", ["read", "write"])
async def test_requests_carry_the_web_app_headers(client, fake, call):
    fake.transactions.append(make_transaction(1, iban="NL00TEST0000000001", name="One"))

    if call == "read":
        await client.vat_codes()
    else:
        await client.create_booking(1, 10, 20)

    headers = fake.requests[-1].headers
    assert headers["x-hamurai"] == "HAMURAI"
    assert headers["device-type"] == "DESKTOP"
