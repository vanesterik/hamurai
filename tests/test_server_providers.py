from datetime import date

import pytest
from conftest import connected, text
from fake_api import make_transaction

from hamurai.suggestions import Entry, LearnedBooking, Suggestions, load, save

pytestmark = pytest.mark.anyio

PROVIDER = "NL00PROV0000000001"
OTHER = "NL00TEST0000000009"
SHOP_A_DIRECT = "NL00TEST0000000005"


def learned(id: int, category_id: int, day: str) -> LearnedBooking:
    return LearnedBooking(id=id, category_id=category_id, vat_code_id=20, date=day)


@pytest.fixture
def path(tmp_path, fake):
    """History on the provider IBAN: Shop A twice (4500), Shop B once (4600). The suggestions
    still hold them as one mixed entry under the provider's IBAN, plus an unrelated entry."""
    history = [
        (1, "Shop A", 10, "2025-03-01"),
        (2, "Shop B", 11, "2025-04-01"),
        (3, "Shop A", 10, "2025-05-01"),
    ]
    ids = {}
    for transaction_id, name, category_id, day in history:
        fake.transactions.append(
            make_transaction(transaction_id, iban=PROVIDER, name=name, entry_date=day)
        )
        ids[transaction_id] = fake.add_booking(transaction_id, category_id, 20)
    fake.transactions.append(make_transaction(9, iban=OTHER, name="Other", entry_date="2025-01-01"))
    other_id = fake.add_booking(9, 11, 20)
    old = Entry(
        iban=PROVIDER,
        name="Shop A",
        always_ask=True,
        bookings=[
            learned(ids[1], 10, "2025-03-01"),
            learned(ids[2], 11, "2025-04-01"),
            learned(ids[3], 10, "2025-05-01"),
        ],
    )
    other = Entry(iban=OTHER, name="Other", bookings=[learned(other_id, 11, "2025-01-01")])
    path = tmp_path / "data" / "suggestions.yaml"
    save(Suggestions(entries=[old, other], provider_ibans=[PROVIDER]), path)
    return path


async def relearn(client, path):
    async with connected(client, path) as session:
        return await session.call_tool("relearn_providers", {})


async def test_relearn_providers_splits_merchants(client, fake, path):
    result = await relearn(client, path)

    assert not result.is_error, text(result)
    data = result.structured_content
    assert sorted((m["name"], m["bookings"], m["category_id"]) for m in data["merchants"]) == [
        ("Shop A", 2, 10),
        ("Shop B", 1, 11),
    ]
    assert (data["bookings"], data["removed_entries"]) == (3, 1)
    suggestions = load(path)
    assert suggestions.by_iban(PROVIDER) is None
    shop_a = suggestions.by_name("Shop A")
    assert shop_a is not None and shop_a.iban is None and not shop_a.always_ask


async def test_relearn_providers_latest_choice_wins(client, fake, path):
    fake.transactions.append(
        make_transaction(4, iban=PROVIDER, name="Shop A", entry_date="2025-06-01")
    )
    fake.add_booking(4, 11, 20)

    await relearn(client, path)

    shop_a = load(path).by_name("Shop A")
    assert shop_a is not None and shop_a.current.category_id == 11


async def test_relearn_keeps_a_later_direct_booking_current(client, fake, path):
    fake.transactions.append(
        make_transaction(5, iban=SHOP_A_DIRECT, name="Shop A", entry_date="2026-02-01")
    )
    direct_id = fake.add_booking(5, 11, 20)
    suggestions = load(path)
    suggestions.entries.append(
        Entry(iban=SHOP_A_DIRECT, name="Shop A", bookings=[learned(direct_id, 11, "2026-02-01")])
    )
    save(suggestions, path)

    await relearn(client, path)

    direct = load(path).by_iban(SHOP_A_DIRECT)
    assert direct is not None
    assert direct.current.category_id == 11 and len(direct.bookings) == 3


async def test_relearn_providers_twice_gives_the_same_file(client, fake, path):
    await relearn(client, path)
    first = path.read_text()

    await relearn(client, path)

    assert path.read_text() == first


async def test_relearn_providers_leaves_other_entries_alone(client, fake, path):
    before = load(path).by_iban(OTHER)

    await relearn(client, path)

    assert load(path).by_iban(OTHER) == before


async def test_relearn_providers_refuses_without_a_list(client, fake, path):
    suggestions = load(path)
    suggestions.provider_ibans = []
    save(suggestions, path)

    result = await relearn(client, path)

    assert result.is_error and "provider_ibans" in text(result)


async def test_relearn_providers_writes_nothing_when_a_read_fails(client, fake, path):
    before = path.read_text()
    fake.fail_routes["/reconciliations"] = [500, 500]

    result = await relearn(client, path)

    assert result.is_error
    assert path.read_text() == before


async def test_book_learns_a_provider_transaction_by_merchant(client, fake, path):
    fake.transactions.append(
        make_transaction(6, iban=PROVIDER, name="Shop C", entry_date="2026-07-02")
    )
    async with connected(client, path) as session:
        proposed = await session.call_tool(
            "propose_bookings", {"start_date": "2026-07-01", "end_date": "2026-07-31"}
        )
        assert not proposed.is_error, text(proposed)
        booked = await session.call_tool(
            "book", {"bookings": [{"transaction_id": 6, "category_id": 10, "vat_code_id": 20}]}
        )

    assert not booked.is_error, text(booked)
    shop_c = load(path).by_name("Shop C")
    assert shop_c is not None and shop_c.iban is None
    assert [(b.category_id, b.date) for b in shop_c.bookings] == [(10, date(2026, 7, 2))]


async def test_undo_after_relearn(client, fake, path):
    await relearn(client, path)
    shop_b = load(path).by_name("Shop B")
    assert shop_b is not None
    [booking_id] = [b.id for b in shop_b.bookings]

    async with connected(client, path) as session:
        result = await session.call_tool("undo_booking", {"booking_id": booking_id})

    assert not result.is_error, text(result)
    assert load(path).by_name("Shop B") is None


async def test_relearn_keeps_always_ask_set_between_runs(client, fake, path):
    await relearn(client, path)
    suggestions = load(path)
    shop_a = suggestions.by_name("Shop A")
    assert shop_a is not None
    shop_a.always_ask = True
    save(suggestions, path)

    await relearn(client, path)

    shop_a = load(path).by_name("Shop A")
    assert shop_a is not None and shop_a.always_ask


async def test_relearn_keeps_bookings_it_cannot_relearn(client, fake, path):
    # A card line once learned into the old provider entry by its name: not in the history.
    suggestions = load(path)
    old = suggestions.entries[0]
    old.bookings.append(learned(8888, 11, "2025-07-01"))
    save(suggestions, path)

    result = await relearn(client, path)

    assert result.structured_content["kept_bookings"] == 1
    shop_a = load(path).by_name("Shop A")
    assert shop_a is not None and 8888 in [b.id for b in shop_a.bookings]


async def test_relearn_flags_a_merchant_booked_more_than_one_way(client, fake, path):
    fake.transactions.append(
        make_transaction(4, iban=PROVIDER, name="Shop A", entry_date="2025-06-01")
    )
    fake.add_booking(4, 11, 20)

    result = await relearn(client, path)

    flags = {m["name"]: m["mixed"] for m in result.structured_content["merchants"]}
    assert flags == {"Shop A": True, "Shop B": False}
