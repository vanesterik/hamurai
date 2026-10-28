import json
from datetime import date, datetime, timedelta

import httpx
import pytest
from conftest import COOKIE
from fake_api import make_file, make_transaction

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
async def test_requests_send_the_configured_app_header(client, fake, call):
    fake.transactions.append(make_transaction(1, iban="NL00TEST0000000001", name="One"))

    if call == "read":
        await client.vat_codes()
    else:
        await client.create_booking(1, 10, 20)

    headers = fake.requests[-1].headers
    assert headers["x-test-app"] == "TEST"
    assert headers["device-type"] == "DESKTOP"


async def test_requests_go_to_the_configured_base_url(client, fake):
    await client.vat_codes()

    url = fake.requests[-1].url
    assert url.host == "hamurai.test"
    assert url.path == "/api/v2/administrations/11111/vat-codes"


async def test_update_booking_sends_the_recorded_body(client, fake):
    fake.transactions.append(make_transaction(1, iban=None, name="Card", amount=-9761))
    booking = await client.create_booking(1, 10, 20)

    updated = await client.update_booking(
        booking.id,
        1,
        amount_cents=-1299,
        category_id=10,
        vat_code_id=21,
        description="ACME SOFTWARE",
        entry_date=date(2026, 9, 21),
    )

    assert json.loads(fake.requests[-1].content) == {
        "amount": -1299,
        "generalLedgerAccountId": 10,
        "vatCodeId": 21,
        "description": "ACME SOFTWARE",
        "entryDate": "2026-09-21",
    }
    assert fake.requests[-1].method == "POST"
    assert fake.requests[-1].url.path.endswith(f"/reconciliations/{booking.id}")
    assert (updated.id, updated.amount_cents, updated.vat_code_id) == (booking.id, -1299, 21)
    assert updated.transaction_id == 1


async def test_create_takes_the_remainder(client, fake):
    fake.transactions.append(make_transaction(1, iban=None, name="Card", amount=-9761))
    first = await client.create_booking(1, 10, 20)
    assert first.amount_cents == -9761
    await client.update_booking(
        first.id,
        1,
        amount_cents=-1299,
        category_id=10,
        vat_code_id=20,
        description="ACME SOFTWARE",
        entry_date=date(2026, 9, 21),
    )
    [transaction] = await client.transactions()
    assert not transaction.is_fully_reconciled

    second = await client.create_booking(1, 11, 21)

    assert second.amount_cents == -8462
    [transaction] = await client.transactions()
    assert transaction.is_fully_reconciled
    assert transaction.number_of_reconciliations == 2


async def test_update_booking_is_never_retried(client, fake):
    fake.transactions.append(make_transaction(1, iban=None, name="Card", amount=-9761))
    booking = await client.create_booking(1, 10, 20)
    fake.fail_updates = [httpx.ReadTimeout("timeout")]

    with pytest.raises(BookingOutcomeUnknown):
        await client.update_booking(
            booking.id,
            1,
            amount_cents=-1299,
            category_id=10,
            vat_code_id=20,
            description="ACME SOFTWARE",
            entry_date=date(2026, 9, 21),
        )

    updates = [r for r in fake.requests if r.url.path.endswith(f"/reconciliations/{booking.id}")]
    assert len(updates) == 1


async def test_files_reads_receipts(client, fake):
    fake.files.append(
        make_file(1, created_at="2026-07-01T10:19:43", amount_cents=875, receipt_date="2026-07-01")
    )

    [file] = await client.files(date(2026, 1, 1))

    assert (file.id, file.amount_cents, file.receipt_date) == (1, 875, date(2026, 7, 1))
    assert not file.is_linked
    assert "SECRET-FILE" not in file.model_dump_json()


async def test_a_file_without_a_mime_type_is_still_read(client, fake):
    without = make_file(1, created_at="2026-07-01T10:00:00")
    del without["mimeType"]
    fake.files += [without, make_file(2, created_at="2026-07-01T09:00:00")]

    files = await client.files(date(2026, 1, 1))

    assert [f.id for f in files] == [1, 2]


async def test_files_pages_by_offset_until_the_date(client, fake):
    start = datetime(2026, 10, 1, 12, 0)
    fake.files += [
        make_file(i, created_at=(start - timedelta(days=i)).isoformat()) for i in range(250)
    ]

    files = await client.files((start - timedelta(days=150)).date())

    offsets = [r.url.params.get("offset", "0") for r in fake.requests]
    assert offsets == ["0", "100"]
    assert len(files) == 200


async def test_files_stops_on_an_empty_page(client, fake):
    fake.files += [make_file(i, created_at="2026-09-01T10:00:00") for i in range(100)]

    files = await client.files(date(2026, 1, 1))

    assert len(files) == 100
    assert [r.url.params.get("offset", "0") for r in fake.requests] == ["0", "100"]


async def test_archived_file_is_not_unlinked(client, fake):
    fake.files.append(
        make_file(1, created_at="2026-07-01T10:00:00", archived_at="2026-08-01T10:00:00")
    )

    [file] = await client.files(date(2026, 1, 1))

    assert file.is_linked


async def test_linked_file(client, fake):
    fake.files.append(make_file(1, created_at="2026-07-01T10:00:00", transaction_id=7))

    [file] = await client.files(date(2026, 1, 1))

    assert file.is_linked and file.transaction_id == 7


async def test_file_without_metadata(client, fake):
    fake.files += [
        make_file(1, created_at="2026-07-01T10:00:00"),
        {**make_file(2, created_at="2026-07-01T09:00:00"), "metadata": {}},
    ]

    files = await client.files(date(2026, 1, 1))

    assert [(f.amount_cents, f.receipt_date) for f in files] == [(None, None), (None, None)]


async def test_attach_file_sends_the_transaction(client, fake):
    fake.transactions.append(make_transaction(1, iban=None, name="Shop"))
    fake.files.append(make_file(5, created_at="2026-07-01T10:00:00", amount_cents=1000))

    file = await client.attach_file(5, 1)

    assert fake.requests[-1].url.path.endswith("/files/5/attach")
    assert json.loads(fake.requests[-1].content) == {"transactionId": 1}
    assert file.transaction_id == 1 and file.is_linked
    [transaction] = await client.transactions()
    assert transaction.has_files


async def test_attach_file_is_never_retried(client, fake):
    fake.transactions.append(make_transaction(1, iban=None, name="Shop"))
    fake.files.append(make_file(5, created_at="2026-07-01T10:00:00", amount_cents=1000))
    fake.fail_attaches = [httpx.ReadTimeout("timeout")]

    with pytest.raises(BookingOutcomeUnknown):
        await client.attach_file(5, 1)

    assert len([r for r in fake.requests if r.url.path.endswith("/attach")]) == 1


async def test_booking_description(client, fake):
    fake.transactions.append(make_transaction(1, iban=None, name="Card", amount=-9761))
    booking = await client.create_booking(1, 10, 20)
    await client.update_booking(
        booking.id,
        1,
        amount_cents=-1299,
        category_id=10,
        vat_code_id=20,
        description="ACME SOFTWARE",
        entry_date=date(2026, 9, 21),
    )

    [read] = await client.bookings_for(1)

    assert read.description == "ACME SOFTWARE"


async def test_files_stops_when_a_page_repeats(client, fake):
    fake.files += [make_file(i, created_at="2026-09-01T10:00:00") for i in range(150)]
    fake.repeat_last_page = True

    files = await client.files(date(2026, 1, 1))

    assert sorted(f.id for f in files) == list(range(150))
    assert len([r for r in fake.requests if r.url.path.endswith("/files")]) <= 3


async def test_unreadable_metadata_counts_as_without_amount(client, fake):
    fake.files += [
        {
            **make_file(1, created_at="2026-07-01T10:00:00"),
            "metadata": {"amountInCents": 875, "date": "01-08-2026"},
        },
        {
            **make_file(2, created_at="2026-07-01T09:00:00"),
            "metadata": {"amountInCents": "abc", "date": "2026-08-01"},
        },
    ]

    files = await client.files(date(2026, 1, 1))

    assert [(f.amount_cents, f.receipt_date) for f in files] == [
        (875, None),
        (None, date(2026, 8, 1)),
    ]


async def test_unreadable_file_hides_its_values(client, fake):
    broken = make_file(1, created_at="2026-07-01T10:00:00")
    del broken["name"]
    fake.files.append(broken)

    with pytest.raises(HamuraiError) as error:
        await client.files(date(2026, 1, 1))

    assert "SECRET-FILE" not in str(error.value)


def _answering(fake, route_suffix: str, change) -> None:
    """Rewrite the fake's JSON answer on one POST route, as Hamurai might send it."""
    respond = fake._respond

    def rewritten(request):
        response = respond(request)
        if request.method == "POST" and request.url.path.endswith(route_suffix):
            return httpx.Response(response.status_code, json=change(response.json()))
        return response

    fake._respond = rewritten


async def test_update_answer_with_null_transaction_info(client, fake):
    fake.transactions.append(make_transaction(1, iban=None, name="Card", amount=-9761))
    booking = await client.create_booking(1, 10, 20)
    _answering(fake, f"/reconciliations/{booking.id}", lambda a: {**a, "transactionInfo": None})

    updated = await client.update_booking(
        booking.id,
        1,
        amount_cents=-1299,
        category_id=10,
        vat_code_id=20,
        description="ACME SOFTWARE",
        entry_date=date(2026, 9, 21),
    )

    assert (updated.transaction_id, updated.amount_cents) == (1, -1299)


async def test_create_answer_includes_transaction_info(client, fake):
    # As recorded from the web app: a create's answer carries transactionInfo too.
    fake.transactions.append(make_transaction(1, iban=None, name="Card", amount=-9761))
    answers = []
    _answering(fake, "/reconciliations", lambda a: answers.append(a) or a)

    booking = await client.create_booking(1, 10, 20)

    assert [a["transactionInfo"]["id"] for a in answers] == [1]
    assert booking.transaction_id == 1
