"""Hamurai's data, with only the fields that are used. Unknown fields are ignored."""

from datetime import date, datetime
from typing import Annotated

from pydantic import AliasPath, BaseModel, BeforeValidator, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="ignore", frozen=True
    )


class CounterAccount(_Model):
    number: str | None = None
    name_account_holder: str | None = None


class Transaction(_Model):
    id: int
    entry_date: date
    amount_cents: int = Field(alias="amount")
    # Hamurai may send null for these, e.g. for a bank fee.
    description: Annotated[str, BeforeValidator(lambda v: v or "")] = ""
    counter_account: Annotated[CounterAccount, BeforeValidator(lambda v: v or {})] = (
        CounterAccount()
    )
    is_fully_reconciled: bool
    number_of_reconciliations: int
    has_files: bool = False


class Category(_Model):
    """A general ledger account ("rubriek"). Codes are not unique; IDs are."""

    id: int
    code: str
    name: str


class VatCode(_Model):
    id: int
    description: str
    percentage: float


class Booking(_Model):
    """A reconciliation of a transaction onto a category and VAT code."""

    id: int
    transaction_id: int = Field(validation_alias=AliasPath("transactionInfo", "id"))
    category_id: int = Field(validation_alias=AliasPath("generalLedgerAccountInfo", "id"))
    vat_code_id: int = Field(validation_alias=AliasPath("vatCode", "id"))
    amount_cents: int = Field(alias="amountInCents")
    description: Annotated[str, BeforeValidator(lambda v: v or "")] = ""


class File(_Model):
    """An uploaded file, such as a receipt. Its download secret is never read."""

    id: int
    created_at: datetime
    name: str
    mime_type: str
    # What Hamurai read from a receipt: a positive amount and the receipt's date.
    amount_cents: int | None = Field(
        default=None, validation_alias=AliasPath("metadata", "amountInCents")
    )
    receipt_date: date | None = Field(default=None, validation_alias=AliasPath("metadata", "date"))
    archived_at: datetime | None = None
    transaction_id: int | None = None
    invoice_id: int | None = None
    order_id: int | None = None
    journal_record_id: int | None = None
    estimate_id: int | None = None
    reminder_id: int | None = None
    billable_item_sheet_id: int | None = None
    bank_transaction_import_id: int | None = None

    @property
    def is_linked(self) -> bool:
        """Linked to anything, or archived: never proposed as a receipt."""
        links = (
            self.transaction_id,
            self.invoice_id,
            self.order_id,
            self.journal_record_id,
            self.estimate_id,
            self.reminder_id,
            self.billable_item_sheet_id,
            self.bank_transaction_import_id,
        )
        return self.archived_at is not None or any(link is not None for link in links)
