"""Hamurai's data, with only the fields that are used. Unknown fields are ignored."""

from datetime import date

from pydantic import AliasPath, BaseModel, ConfigDict, Field
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
    description: str = ""
    counter_account: CounterAccount = CounterAccount()
    is_fully_reconciled: bool
    number_of_reconciliations: int


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
