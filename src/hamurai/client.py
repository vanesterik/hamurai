"""The only unit that talks to Hamurai over HTTP.

Exceptions raised here never carry the request or its headers, so the cookie cannot leak
through them.
"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from typing import Any

import anyio
import httpx

from hamurai.config import Config
from hamurai.models import Booking, Category, Transaction, VatCode

BASE_URL = "https://app.hamurai.example"
PAGE_SIZE = 500

# Sent by Hamurai's web app on every request. Reads work without them, but bookings
# are refused without them: "You have no access to this action." (verified live).
WEB_APP_HEADERS = {"x-hamurai": "HAMURAI", "device-type": "DESKTOP"}


class SessionExpired(Exception):
    """Hamurai answered 401: the session cookie is no longer valid."""


class BookingOutcomeUnknown(Exception):
    """A booking request got no answer; it may or may not have been booked."""


class HamuraiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Hamurai answered {status}: {message}")
        self.status = status
        self.message = message


class HamuraiClient:
    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._prefix = f"/api/v2/administrations/{config.administration_id}"
        self._journal_id = config.journal_id
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Cookie": config.cookie.get_secret_value(),
                "Accept": "application/json",
                **WEB_APP_HEADERS,
            },
            timeout=30.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def transactions(
        self, start: date | None = None, end: date | None = None
    ) -> list[Transaction]:
        params: dict[str, Any] = {"journalId": self._journal_id, "size": PAGE_SIZE}
        if start is not None:
            params["startDate"] = start.isoformat()
        if end is not None:
            params["endDate"] = end.isoformat()
        transactions: list[Transaction] = []
        page = 0
        while True:
            body = await self._get("/transactions", {**params, "page": page})
            transactions += [Transaction.model_validate(t) for t in body["content"]]
            if body["page"]["last"]:
                return transactions
            page += 1

    async def bookings_for(self, transaction_id: int) -> list[Booking]:
        """The bookings onto a category; others (e.g. onto an invoice) are left out."""
        body = await self._get("/reconciliations", {"transactionId": transaction_id})
        return [
            Booking.model_validate(b)
            for b in body
            if b.get("type") == "GENERAL_LEDGER_ACCOUNT"
            and b.get("generalLedgerAccountInfo")
            and b.get("vatCode")
        ]

    async def bookings_for_many(
        self,
        transaction_ids: Sequence[int],
        concurrency: int = 4,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> dict[int, list[Booking]]:
        found: dict[int, list[Booking]] = {}
        limit = anyio.Semaphore(concurrency)

        async def fetch(transaction_id: int) -> None:
            async with limit:
                found[transaction_id] = await self.bookings_for(transaction_id)
            if on_progress is not None:
                await on_progress(len(found), len(transaction_ids))

        try:
            async with anyio.create_task_group() as group:
                for transaction_id in transaction_ids:
                    group.start_soon(fetch, transaction_id)
        except ExceptionGroup as errors:
            # The task group wraps failures; callers expect the error itself, an expired
            # session first of all.
            error: BaseException = (errors.subgroup(SessionExpired) or errors).exceptions[0]
            while isinstance(error, ExceptionGroup):
                error = error.exceptions[0]
            raise error from None
        return found

    async def categories(self) -> list[Category]:
        return [Category.model_validate(c) for c in await self._get("/general-ledger-accounts")]

    async def vat_codes(self) -> list[VatCode]:
        return [VatCode.model_validate(v) for v in await self._get("/vat-codes")]

    async def create_booking(
        self, transaction_id: int, category_id: int, vat_code_id: int
    ) -> Booking:
        body = {
            "type": "GENERAL_LEDGER_ACCOUNT",
            "transactionId": transaction_id,
            "generalLedgerAccountId": category_id,
            "vatCodeId": vat_code_id,
            "suggestion": None,
            "suggestionAccepted": False,
        }
        # Never retried: after a timeout nobody knows whether the booking was made.
        try:
            response = await self._http.post(f"{self._prefix}/reconciliations", json=body)
        except httpx.ConnectError:
            raise HamuraiError(0, "could not connect to Hamurai") from None
        except httpx.TransportError:
            raise BookingOutcomeUnknown() from None
        _checked(response)
        try:
            # The POST answer has no transactionInfo, so the transaction ID comes from the request.
            return Booking.model_validate(
                {**response.json(), "transactionInfo": {"id": transaction_id}}
            )
        except ValueError:
            raise BookingOutcomeUnknown() from None

    async def delete_booking(self, booking_id: int) -> None:
        try:
            response = await self._http.delete(f"{self._prefix}/reconciliations/{booking_id}")
        except httpx.TransportError:
            raise HamuraiError(0, "no answer from Hamurai") from None
        _checked(response)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
        try:
            response = await self._http.get(self._prefix + path, params=params)
        except httpx.TransportError:
            response = await self._http.get(self._prefix + path, params=params)
        return _checked(response).json()


def _checked(response: httpx.Response) -> httpx.Response:
    if response.status_code == 401:
        raise SessionExpired()
    if response.is_success:
        return response
    try:
        message = response.json()["error"]["message"]
    except ValueError, KeyError, TypeError:
        message = f"HTTP {response.status_code}"
    raise HamuraiError(response.status_code, message)
