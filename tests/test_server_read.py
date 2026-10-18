from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import pytest
from conftest import COOKIE
from fake_api import make_transaction
from mcp import Client

from hamurai.server import SESSION_EXPIRED, build_server

pytestmark = pytest.mark.anyio

IBAN = "NL00TEST0000000001"
OTHER_IBAN = "NL00TEST0000000002"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "data" / "suggestions.yaml"


@asynccontextmanager
async def connected(client, path) -> AsyncGenerator[Client]:
    async with Client(build_server(client, path)) as session:
        yield session


def text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


def booked_history(fake) -> None:
    """Acme always 4500; Mixed booked to 4500 twice and 4600 once."""
    for i, (iban, name, category) in enumerate(
        [(IBAN, "Acme", 10), (IBAN, "Acme", 10), (OTHER_IBAN, "Mixed", 10)]
        + [(OTHER_IBAN, "Mixed", 10), (OTHER_IBAN, "Mixed", 11)],
        start=1,
    ):
        fake.transactions.append(
            make_transaction(i, iban=iban, name=name, entry_date=f"2026-04-{i:02d}")
        )
        fake.add_booking(i, category, 20)


async def seed_and_save(session) -> None:
    seeded = await session.call_tool("seed_suggestions", {})
    assert not seeded.is_error, text(seeded)
    saved = await session.call_tool(
        "save_suggestions",
        {"resolutions": [{"key": f"iban:{OTHER_IBAN}", "category_id": 10, "vat_code_id": 20}]},
    )
    assert not saved.is_error, text(saved)


async def test_lists_categories_and_vat_codes(client, path):
    async with connected(client, path) as session:
        categories = await session.call_tool("list_categories", {})
        vat_codes = await session.call_tool("list_vat_codes", {})

    assert categories.structured_content["result"][0]["name"] == "Kantoorkosten"
    assert vat_codes.structured_content["result"][0]["description"] == "21% hoog"


async def test_propose_bookings_groups_the_period(client, fake, path):
    booked_history(fake)
    fake.transactions += [
        make_transaction(10, iban=IBAN, name="Acme", entry_date="2026-07-02"),
        make_transaction(11, iban=None, name="mixed", entry_date="2026-07-03"),
        make_transaction(12, iban=None, name="Stranger", entry_date="2026-07-04"),
        make_transaction(13, iban=None, name="Outside", entry_date="2026-10-01"),
    ]

    async with connected(client, path) as session:
        await seed_and_save(session)
        result = await session.call_tool(
            "propose_bookings", {"start_date": "2026-07-01", "end_date": "2026-09-30"}
        )

    assert not result.is_error, text(result)
    proposals = result.structured_content["proposals"]
    assert [p["transaction"]["id"] for p in proposals["confident"]] == [10]
    assert proposals["confident"][0]["evidence"].startswith("IBAN match, 2× booked as 4500")
    assert [p["transaction"]["id"] for p in proposals["uncertain"]] == [11]
    assert [p["transaction"]["id"] for p in proposals["unknown"]] == [12]
    assert result.structured_content["hint"] is None


async def test_propose_bookings_rejects_reversed_dates(client, path):
    async with connected(client, path) as session:
        result = await session.call_tool(
            "propose_bookings", {"start_date": "2026-09-30", "end_date": "2026-07-01"}
        )

    assert result.is_error
    assert "end_date" in text(result)


async def test_propose_bookings_hints_at_seeding_when_empty(client, fake, path):
    fake.transactions.append(make_transaction(1, iban=IBAN, name="Acme"))

    async with connected(client, path) as session:
        result = await session.call_tool(
            "propose_bookings", {"start_date": "2026-07-01", "end_date": "2026-09-30"}
        )

    assert "seed_suggestions" in result.structured_content["hint"]
    assert len(result.structured_content["proposals"]["unknown"]) == 1


async def test_seed_reports_flagged_and_writes_nothing(client, fake, path):
    booked_history(fake)

    async with connected(client, path) as session:
        result = await session.call_tool("seed_suggestions", {})

    summary = result.structured_content
    assert (summary["entries"], summary["bookings"]) == (2, 5)
    [flagged] = summary["flagged"]
    assert flagged["key"] == f"iban:{OTHER_IBAN}"
    assert [(p["label"], p["count"], p["last_seen"]) for p in flagged["pairs"]] == [
        ("4500 Kantoorkosten / 21% hoog", 2, "2026-04-04"),
        ("4600 Reiskosten / 21% hoog", 1, "2026-04-05"),
    ]
    assert not path.exists()


async def test_seed_skips_split_transactions_without_reading_them(client, fake, path):
    fake.transactions.append(make_transaction(1, iban=IBAN, name="Acme"))
    fake.add_booking(1, 10, 20)
    fake.add_booking(1, 11, 20)

    async with connected(client, path) as session:
        result = await session.call_tool("seed_suggestions", {})

    assert result.structured_content["skipped_split"] == 1
    assert not any(r.url.path.endswith("/reconciliations") for r in fake.requests)


async def test_seed_then_save_writes_the_file(client, fake, path):
    booked_history(fake)

    async with connected(client, path) as session:
        await seed_and_save(session)

    assert path.read_text().count("{id: ") == 4


async def test_save_suggestions_without_seeding_is_refused(client, path):
    async with connected(client, path) as session:
        result = await session.call_tool("save_suggestions", {"resolutions": []})

    assert result.is_error
    assert "seed_suggestions" in text(result)


async def test_save_suggestions_refuses_unresolved(client, fake, path):
    booked_history(fake)

    async with connected(client, path) as session:
        await session.call_tool("seed_suggestions", {})
        result = await session.call_tool("save_suggestions", {"resolutions": []})

    assert result.is_error
    assert f"iban:{OTHER_IBAN}" in text(result)
    assert not path.exists()


async def test_save_suggestions_refuses_to_overwrite(client, fake, path):
    booked_history(fake)
    path.parent.mkdir(parents=True)
    path.write_text("entries: []\n")

    async with connected(client, path) as session:
        await session.call_tool("seed_suggestions", {})
        result = await session.call_tool(
            "save_suggestions",
            {"resolutions": [{"key": f"iban:{OTHER_IBAN}", "category_id": 10, "vat_code_id": 20}]},
        )

    assert result.is_error
    assert "delete it" in text(result)
    assert "run seed_suggestions again" in text(result)
    assert path.read_text() == "entries: []\n"


async def test_corrupt_file_is_reported(client, path):
    path.parent.mkdir(parents=True)
    path.write_text("entries: [unclosed\n")

    async with connected(client, path) as session:
        result = await session.call_tool(
            "propose_bookings", {"start_date": "2026-07-01", "end_date": "2026-09-30"}
        )

    assert result.is_error
    assert "line" in text(result)


async def test_session_expired_message(client, fake, path):
    fake.expired = True

    async with connected(client, path) as session:
        result = await session.call_tool("list_vat_codes", {})

    assert result.is_error
    assert SESSION_EXPIRED in text(result)


READ_CALLS = [
    ("list_categories", {}),
    ("list_vat_codes", {}),
    ("propose_bookings", {"start_date": "2026-07-01", "end_date": "2026-09-30"}),
    ("seed_suggestions", {}),
    ("save_suggestions", {"resolutions": []}),
]


@pytest.mark.parametrize("failure", [None, "expired", 500])
async def test_cookie_never_in_output(client, fake, path, failure):
    booked_history(fake)
    secret = COOKIE.split("=", 1)[1]

    async with connected(client, path) as session:
        for name, arguments in READ_CALLS:
            if failure == "expired":
                fake.expired = True
            elif failure == 500:
                fake.fail_next = [500, 500]
            result = await session.call_tool(name, arguments)
            assert secret not in text(result)
            assert secret not in str(result.structured_content)


@pytest.mark.parametrize(
    ("failures", "message"),
    [
        ([401], SESSION_EXPIRED),
        ([httpx.ConnectError("down"), httpx.ConnectError("down")], "Could not reach Hamurai"),
    ],
)
async def test_seed_failing_partway_gives_a_clear_message(client, fake, path, failures, message):
    booked_history(fake)
    fake.fail_routes = {"/reconciliations": failures}

    async with connected(client, path) as session:
        result = await session.call_tool("seed_suggestions", {})

    assert result.is_error
    assert message in text(result)


async def test_seed_skips_bookings_not_onto_a_category(client, fake, path):
    booked_history(fake)
    fake.transactions.append(make_transaction(9, iban=IBAN, name="Acme", entry_date="2026-04-09"))
    fake.add_other_booking(9)

    async with connected(client, path) as session:
        result = await session.call_tool("seed_suggestions", {})

    assert not result.is_error, text(result)
    assert result.structured_content["skipped_other"] == 1
    assert result.structured_content["bookings"] == 5


async def test_propose_bookings_with_null_fields(client, fake, path):
    transaction = make_transaction(1, iban=None, name=None)
    transaction["counterAccount"] = None
    transaction["description"] = None
    fake.transactions.append(transaction)

    async with connected(client, path) as session:
        result = await session.call_tool(
            "propose_bookings", {"start_date": "2026-07-01", "end_date": "2026-09-30"}
        )

    assert not result.is_error, text(result)
    assert len(result.structured_content["proposals"]["unknown"]) == 1


async def test_save_suggestions_with_always_ask(client, fake, path):
    booked_history(fake)
    fake.transactions.append(
        make_transaction(10, iban=OTHER_IBAN, name="Mixed", entry_date="2026-07-02")
    )

    async with connected(client, path) as session:
        await session.call_tool("seed_suggestions", {})
        saved = await session.call_tool(
            "save_suggestions", {"resolutions": [{"key": f"iban:{OTHER_IBAN}", "always_ask": True}]}
        )
        result = await session.call_tool(
            "propose_bookings", {"start_date": "2026-07-01", "end_date": "2026-09-30"}
        )

    assert not saved.is_error, text(saved)
    [proposal] = result.structured_content["proposals"]["uncertain"]
    assert "always ask" in proposal["evidence"]


async def test_list_suggestions_groups_what_was_learned(client, fake, path):
    async with connected(client, path) as session:
        booked_history(fake)
        await seed_and_save(session)
        by_category = await session.call_tool("list_suggestions", {})
        by_match = await session.call_tool(
            "list_suggestions", {"group_by": "match", "text": "acme"}
        )
        limited = await session.call_tool("list_suggestions", {"limit": 1})
        refused = await session.call_tool("list_suggestions", {"group_by": "colour"})

    assert not by_category.is_error, text(by_category)
    data = by_category.structured_content
    assert data["entries"] == 2
    assert [g["title"] for g in data["groups"]] == ["4500 Kantoorkosten / 21% hoog"]
    assert [e["name"] for e in by_match.structured_content["groups"][0]["entries"]] == ["Acme"]
    assert [g["more"] for g in limited.structured_content["groups"]] == [1]
    assert refused.is_error
    assert [r for r in fake.requests if r.method != "GET"] == []
