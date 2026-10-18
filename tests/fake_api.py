"""An in-memory Hamurai API, served to the client through `httpx.MockTransport`.

All data is invented. Response shapes follow the spec's API section.
"""

import json
import re
from typing import Any

import anyio
import httpx

ADMINISTRATION_ID = 11111
JOURNAL_ID = 22222
PREFIX = f"/api/v2/administrations/{ADMINISTRATION_ID}"

type Failure = BaseException | int | tuple[str, BaseException | int] | None

CATEGORIES = [
    {"id": 10, "code": "4500", "name": "Kantoorkosten", "type": "COSTS"},
    {"id": 11, "code": "4600", "name": "Reiskosten", "type": "COSTS"},
]
VAT_CODES = [
    {"id": 20, "description": "21% hoog", "percentage": 21.0, "type": "EXPENSES"},
    {"id": 21, "description": "Geen / Vrijgesteld", "percentage": 0.0, "type": "EXPENSES"},
]


def make_transaction(
    id: int,
    *,
    iban: str | None,
    name: str | None,
    description: str = "",
    entry_date: str = "2026-07-01",
    amount: int = -1000,
) -> dict[str, Any]:
    return {
        "id": id,
        "journalId": JOURNAL_ID,
        "entryDate": entry_date,
        "amount": amount,
        "description": description,
        "counterAccount": {"number": iban, "nameAccountHolder": name},
        "isFullyReconciled": False,
        "numberOfReconciliations": 0,
        "hasSuggestions": False,
        "hasFiles": False,
    }


def make_file(
    id: int,
    *,
    created_at: str,
    amount_cents: int | None = None,
    receipt_date: str | None = None,
    transaction_id: int | None = None,
    archived_at: str | None = None,
) -> dict[str, Any]:
    """A file as the files list returns it. Without an amount or date, metadata is null."""
    metadata = None
    if amount_cents is not None or receipt_date is not None:
        metadata = {"ocrResultId": 1, "amountInCents": amount_cents, "date": receipt_date}
    return {
        "id": id,
        "administrationId": ADMINISTRATION_ID,
        "createdAt": created_at,
        "createdByUsername": "Test User",
        "name": f"Scan {id}",
        "mimeType": "image/jpeg",
        "sizeInBytes": 1000,
        "metadata": metadata,
        "secret": "SECRET-FILE",
        "archivedAt": archived_at,
        "transactionId": transaction_id,
        "invoiceId": None,
        "orderId": None,
        "reminderId": None,
        "estimateId": None,
        "journalRecordId": None,
        "billableItemSheetId": None,
        "bankTransactionImportId": None,
        "type": "STANDARD_FILE",
        "status": "PROCESSED",
    }


class FakeHamurai:
    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []
        self.bookings: dict[int, dict[str, Any]] = {}
        self.categories = list(CATEGORIES)
        self.vat_codes = list(VAT_CODES)
        self.requests: list[httpx.Request] = []
        self.expired = False
        self.expire_after_posts: int | None = None
        self.fail_next: list[Exception | int] = []
        self.fail_posts: list[Exception | int | httpx.Response] = []
        self.fail_routes: dict[str, list[Exception | int]] = {}
        # One entry per request on that route; None lets the request through, and
        # ("after", failure) applies the request before failing, as when an answer is lost.
        self.fail_creates: list[Failure] = []
        self.fail_updates: list[Failure] = []
        self.fail_deletes: list[Failure] = []
        self.fail_attaches: list[Failure] = []
        self.files: list[dict[str, Any]] = []
        self.repeat_last_page = False
        self.ignore_update_amounts = False
        self.delay = 0.0
        self.in_flight = 0
        self.max_in_flight = 0
        self._next_booking_id = 9000

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def add_booking(self, transaction_id: int, category_id: int, vat_code_id: int) -> int:
        """Book a transaction directly, as if done in the web app. Returns the booking ID.

        Like Hamurai, a new booking gets the transaction's whole unbooked remainder.
        """
        transaction = self._transaction(transaction_id)
        assert transaction is not None
        remainder = transaction["amount"] - sum(
            b["amountInCents"] for b in self._bookings_on(transaction_id)
        )
        self._next_booking_id += 1
        booking_id = self._next_booking_id
        category = next(c for c in self.categories if c["id"] == category_id)
        vat_code = next(v for v in self.vat_codes if v["id"] == vat_code_id)
        self.bookings[booking_id] = {
            "type": "GENERAL_LEDGER_ACCOUNT",
            "id": booking_id,
            "transactionInfo": {
                "id": transaction_id,
                "journalId": JOURNAL_ID,
                "amountInCents": transaction["amount"],
                "description": transaction["description"],
                "counterAccount": transaction["counterAccount"],
            },
            "generalLedgerAccountInfo": {
                "id": category_id,
                "type": category["type"],
                "name": category["name"],
            },
            "entryDate": transaction["entryDate"],
            "amountInCents": remainder,
            "description": transaction["description"],
            "vatCode": dict(vat_code),
        }
        self._reconcile(transaction)
        return booking_id

    def update_booking(self, booking_id: int, body: dict[str, Any]) -> dict[str, Any]:
        booking = self.bookings[booking_id]
        category = next(c for c in self.categories if c["id"] == body["generalLedgerAccountId"])
        vat_code = next(v for v in self.vat_codes if v["id"] == body["vatCodeId"])
        # As if Hamurai answered 200 but kept the amount it had.
        if not self.ignore_update_amounts:
            booking["amountInCents"] = body["amount"]
        booking["generalLedgerAccountInfo"] = {
            "id": category["id"],
            "type": category["type"],
            "name": category["name"],
        }
        booking["vatCode"] = dict(vat_code)
        booking["description"] = body["description"]
        booking["entryDate"] = body["entryDate"]
        transaction = self._transaction(booking["transactionInfo"]["id"])
        assert transaction is not None
        self._reconcile(transaction)
        return booking

    def _bookings_on(self, transaction_id: int) -> list[dict[str, Any]]:
        return [b for b in self.bookings.values() if b["transactionInfo"]["id"] == transaction_id]

    def _reconcile(self, transaction: dict[str, Any]) -> None:
        """Fully reconciled once the bookings add up to the transaction's amount."""
        bookings = self._bookings_on(transaction["id"])
        transaction["numberOfReconciliations"] = len(bookings)
        transaction["isFullyReconciled"] = bool(bookings) and (
            sum(b["amountInCents"] for b in bookings) == transaction["amount"]
        )

    def add_other_booking(self, transaction_id: int) -> int:
        """Reconcile a transaction against something other than a category, e.g. an invoice."""
        transaction = self._transaction(transaction_id)
        assert transaction is not None
        self._next_booking_id += 1
        self.bookings[self._next_booking_id] = {
            "type": "SALES_INVOICE",
            "id": self._next_booking_id,
            "transactionInfo": {"id": transaction_id, "journalId": JOURNAL_ID},
            "amountInCents": transaction["amount"],
            "generalLedgerAccountInfo": None,
            "vatCode": None,
        }
        transaction["numberOfReconciliations"] += 1
        transaction["isFullyReconciled"] = True
        return self._next_booking_id

    def _transaction(self, transaction_id: int) -> dict[str, Any] | None:
        return next((t for t in self.transactions if t["id"] == transaction_id), None)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await anyio.sleep(self.delay)
            failure = self._own_failure(request)
            if isinstance(failure, tuple):
                self._respond(request)
                failure = failure[1]
            if isinstance(failure, BaseException):
                raise failure
            if failure is not None:
                return _error(failure, f"Fake error {failure}")
            return self._respond(request)
        finally:
            self.in_flight -= 1

    def _own_failure(self, request: httpx.Request) -> Failure:
        route = request.url.path.removeprefix(PREFIX)
        booking_route = re.fullmatch(r"/reconciliations/\d+", route) is not None
        own = (
            self.fail_creates
            if request.method == "POST" and route == "/reconciliations"
            else self.fail_updates
            if request.method == "POST" and booking_route
            else self.fail_deletes
            if request.method == "DELETE" and booking_route
            else self.fail_attaches
            if request.method == "POST" and route.endswith("/attach")
            else None
        )
        return own.pop(0) if own else None

    def _respond(self, request: httpx.Request) -> httpx.Response:
        route = request.url.path.removeprefix(PREFIX)
        failures = self.fail_posts if request.method == "POST" else self.fail_routes.get(route, [])
        if self.fail_next or failures:
            failure = (self.fail_next or failures).pop(0)
            if isinstance(failure, Exception):
                raise failure
            if isinstance(failure, httpx.Response):
                return failure
            return _error(failure, f"Fake error {failure}")
        if self.expired:
            return _error(401, "Not logged in", code=2003)
        if request.method == "POST" and self.expire_after_posts is not None:
            if self.expire_after_posts == 0:
                self.expired = True
                return _error(401, "Not logged in", code=2003)
            self.expire_after_posts -= 1

        path = request.url.path
        if not path.startswith(PREFIX + "/"):
            return _error(404, "Unknown administration")
        route = path.removeprefix(PREFIX)
        params = request.url.params

        if request.method == "GET" and route == "/transactions":
            return self._list_transactions(params)
        if request.method == "GET" and route == "/reconciliations":
            if "transactionId" not in params:
                return _error(400, "transactionId is required")
            transaction_id = int(params["transactionId"])
            found = [
                b for b in self.bookings.values() if b["transactionInfo"]["id"] == transaction_id
            ]
            return httpx.Response(200, json=found)
        if request.method == "GET" and route == "/files":
            ordered = sorted(self.files, key=lambda f: f["createdAt"], reverse=True)
            offset = int(params.get("offset", "0"))
            if self.repeat_last_page and offset >= len(ordered):
                # As Hamurai might: past the end, the last page again instead of nothing.
                offset = max(0, (len(ordered) - 1) // 100 * 100)
            return httpx.Response(200, json=ordered[offset : offset + 100])
        if request.method == "POST" and (match := re.fullmatch(r"/files/(\d+)/attach", route)):
            file = next((f for f in self.files if f["id"] == int(match[1])), None)
            body = json.loads(request.content)
            transaction = self._transaction(body["transactionId"])
            if file is None or transaction is None:
                return _error(404, "Unknown file or transaction")
            file["transactionId"] = transaction["id"]
            transaction["hasFiles"] = True
            return httpx.Response(200, json=file)
        if request.method == "GET" and route == "/general-ledger-accounts":
            return httpx.Response(200, json=self.categories)
        if request.method == "GET" and route == "/vat-codes":
            return httpx.Response(200, json=self.vat_codes)
        if request.method == "POST" and route == "/reconciliations":
            body = json.loads(request.content)
            if self._transaction(body["transactionId"]) is None:
                return _error(404, "Unknown transaction")
            booking_id = self.add_booking(
                body["transactionId"], body["generalLedgerAccountId"], body["vatCodeId"]
            )
            # As recorded from the web app: the answer is the booking, transactionInfo included.
            return httpx.Response(200, json=self.bookings[booking_id])
        if request.method == "POST" and (match := re.fullmatch(r"/reconciliations/(\d+)", route)):
            if int(match[1]) not in self.bookings:
                return _error(404, "Unknown booking")
            return httpx.Response(
                200, json=self.update_booking(int(match[1]), json.loads(request.content))
            )
        if request.method == "DELETE" and (match := re.fullmatch(r"/reconciliations/(\d+)", route)):
            booking = self.bookings.pop(int(match[1]), None)
            if booking is None:
                return _error(404, "Unknown booking")
            transaction = self._transaction(booking["transactionInfo"]["id"])
            assert transaction is not None
            self._reconcile(transaction)
            return httpx.Response(204)
        return _error(404, "Unknown route")

    def _list_transactions(self, params: httpx.QueryParams) -> httpx.Response:
        if params.get("journalId") != str(JOURNAL_ID):
            return _error(400, "Unknown journal")
        selected = [
            t
            for t in self.transactions
            if params.get("startDate", "0000") <= t["entryDate"] <= params.get("endDate", "9999")
        ]
        selected.sort(key=lambda t: t["entryDate"], reverse=True)
        page, size = int(params["page"]), int(params["size"])
        content = selected[page * size : (page + 1) * size]
        total_pages = max(1, -(-len(selected) // size))
        return httpx.Response(
            200,
            json={
                "content": content,
                "page": {
                    "pageNumber": page,
                    "pageSize": size,
                    "totalPages": total_pages,
                    "totalElements": len(selected),
                    "first": page == 0,
                    "last": page >= total_pages - 1,
                },
            },
        )


def _error(status: int, message: str, code: int = 1000) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": code, "message": message, "httpStatus": str(status)}}
    )
