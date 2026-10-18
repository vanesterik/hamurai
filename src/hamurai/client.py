"""The only unit that talks to Hamurai over HTTP.

Exceptions raised here never carry the request or its headers, so the cookie cannot leak
through them.
"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from typing import Any

import anyio
import httpx
from pydantic import ValidationError

from hamurai.config import Config
from hamurai.models import Booking, Category, File, Transaction, VatCode

BASE_URL = "https://app.hamurai.example"
PAGE_SIZE = 500
# The files list comes 100 at a time, whatever is asked for.
FILE_PAGE = 100
MAX_FILE_PAGES = 1000

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

    async def files(self, since: date) -> list[File]:
        """The files, newest first, back to the first page holding one created before `since`.

        Hamurai pages them by `offset`, 100 at a time, and ignores page sizes and filters.
        """
        files: dict[int, File] = {}
        for page_number in range(MAX_FILE_PAGES):
            body = await self._get("/files", {"offset": page_number * FILE_PAGE})
            try:
                page = [File.model_validate(f) for f in body]
            except ValidationError:
                # Never pydantic's own message: it quotes the file, download secret included.
                raise HamuraiError(200, "a file in the list could not be read") from None
            new = [f for f in page if f.id not in files]
            files |= {f.id: f for f in new}
            # A short page, or one with nothing new, is the end, however Hamurai pages.
            if len(page) < FILE_PAGE or not new or any(f.created_at.date() < since for f in page):
                return list(files.values())
        raise HamuraiError(200, f"the file list went on for more than {MAX_FILE_PAGES} pages")

    async def attach_file(self, file_id: int, transaction_id: int) -> File:
        """Link a file, such as a receipt, to a bank transaction."""
        # Never retried, like a booking.
        try:
            response = await self._http.post(
                f"{self._prefix}/files/{file_id}/attach", json={"transactionId": transaction_id}
            )
        except httpx.ConnectError:
            raise HamuraiError(0, "could not connect to Hamurai") from None
        except httpx.TransportError:
            raise BookingOutcomeUnknown() from None
        _checked(response)
        try:
            return File.model_validate(response.json())
        except ValueError:
            raise BookingOutcomeUnknown() from None

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
            # The transaction ID comes from the request, whatever the answer says of it.
            return Booking.model_validate(
                {**response.json(), "transactionInfo": {"id": transaction_id}}
            )
        except ValueError:
            raise BookingOutcomeUnknown() from None

    async def update_booking(
        self,
        booking_id: int,
        transaction_id: int,
        *,
        amount_cents: int,
        category_id: int,
        vat_code_id: int,
        description: str,
        entry_date: date,
    ) -> Booking:
        """Set a booking's amount, as the web app does when splitting a transaction."""
        body = {
            "amount": amount_cents,
            "generalLedgerAccountId": category_id,
            "vatCodeId": vat_code_id,
            "description": description,
            "entryDate": entry_date.isoformat(),
        }
        # Never retried, like a create.
        try:
            response = await self._http.post(
                f"{self._prefix}/reconciliations/{booking_id}", json=body
            )
        except httpx.ConnectError:
            raise HamuraiError(0, "could not connect to Hamurai") from None
        except httpx.TransportError:
            raise BookingOutcomeUnknown() from None
        _checked(response)
        try:
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
