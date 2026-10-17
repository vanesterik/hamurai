"""Matching unlinked receipts to a period's expenses. Pure logic, no I/O.

A receipt matches an expense by amount and date only: Hamurai keeps no supplier. A match is
proposed as confident only when nothing else could fit it.
"""

from collections.abc import Hashable, Mapping, Sequence
from datetime import date, timedelta

from pydantic import BaseModel

from hamurai.models import Booking, File, Transaction

# How far a receipt's date may lie before and after a bank transaction's date.
BANK_WINDOW = (timedelta(days=30), timedelta(days=3))
# A split line carries the collection's date, which comes up to ~45 days after the purchase.
SPLIT_WINDOW = timedelta(days=45)

# A group of targets: the transaction, the amount for split lines, and an index otherwise.
type GroupKey = tuple[int, int | None, int]


class Target(BaseModel, frozen=True):
    """An expense a receipt could belong to: a bank transaction, or one line of a split."""

    transaction_id: int
    date: date
    amount_cents: int
    counterparty: str
    split_line: bool


class Receipt(BaseModel, frozen=True):
    file_id: int
    name: str
    date: date
    amount_cents: int


class Pair(BaseModel):
    receipt: Receipt
    target: Target


class Ambiguous(BaseModel):
    """Receipts and targets that could pair in more than one way: the user picks."""

    receipts: list[Receipt]
    targets: list[Target]


class ReceiptProposal(BaseModel):
    confident: list[Pair] = []
    ambiguous: list[Ambiguous] = []
    unmatched: list[Receipt] = []
    without_amount: int = 0


def targets(
    transactions: Sequence[Transaction], splits: Mapping[int, Sequence[Booking]]
) -> list[Target]:
    """Every expense: a split's negative lines, and the other transactions without files."""
    result = []
    for transaction in transactions:
        if transaction.amount_cents >= 0:
            continue
        if transaction.id in splits:
            # A split's files don't say which line they belong to, so its lines always count.
            result += [
                Target(
                    transaction_id=transaction.id,
                    date=transaction.entry_date,
                    amount_cents=booking.amount_cents,
                    counterparty=booking.description,
                    split_line=True,
                )
                for booking in splits[transaction.id]
                if booking.amount_cents < 0
            ]
        elif not transaction.has_files:
            result.append(
                Target(
                    transaction_id=transaction.id,
                    date=transaction.entry_date,
                    amount_cents=transaction.amount_cents,
                    counterparty=transaction.counter_account.name_account_holder
                    or transaction.description,
                    split_line=False,
                )
            )
    return result


def match(targets: Sequence[Target], files: Sequence[File]) -> ReceiptProposal:
    """Pair the unlinked receipts with the targets they fit."""
    proposal = ReceiptProposal()
    receipts = []
    for file in files:
        if file.is_linked:
            continue
        if file.amount_cents is None or file.receipt_date is None:
            proposal.without_amount += 1
            continue
        receipts.append(
            Receipt(
                file_id=file.id,
                name=file.name,
                date=file.receipt_date,
                amount_cents=file.amount_cents,
            )
        )

    # Lines of one split with the same amount are one group: any of them links the same way.
    groups: dict[GroupKey, list[Target]] = {}
    for n, target in enumerate(targets):
        if target.split_line:
            key = (target.transaction_id, target.amount_cents, -1)
        else:
            key = (target.transaction_id, None, n)
        groups.setdefault(key, []).append(target)

    edges = {
        receipt.file_id: [key for key, group in groups.items() if _fits(receipt, group[0])]
        for receipt in receipts
    }

    # Connected components of receipts and groups, by union-find over the edges.
    parent: dict[Hashable, Hashable] = {}

    def find(node: Hashable) -> Hashable:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for file_id, keys in edges.items():
        for key in keys:
            parent[find(("receipt", file_id))] = find(("group", key))

    components: dict[Hashable, tuple[list[Receipt], list[GroupKey]]] = {}
    for receipt in receipts:
        if not edges[receipt.file_id]:
            proposal.unmatched.append(receipt)
            continue
        found = components.setdefault(find(("receipt", receipt.file_id)), ([], []))
        found[0].append(receipt)
    for key in groups:
        root = find(("group", key))
        if root in components:
            components[root][1].append(key)

    for found_receipts, keys in components.values():
        if len(keys) == 1 and len(found_receipts) <= len(groups[keys[0]]):
            ordered = sorted(found_receipts, key=lambda r: r.date)
            proposal.confident += [
                Pair(receipt=r, target=t) for r, t in zip(ordered, groups[keys[0]], strict=False)
            ]
        else:
            proposal.ambiguous.append(
                Ambiguous(
                    receipts=found_receipts,
                    targets=[target for key in keys for target in groups[key]],
                )
            )
    return proposal


def _fits(receipt: Receipt, target: Target) -> bool:
    if receipt.amount_cents != -target.amount_cents:
        return False
    if target.split_line:
        return target.date - SPLIT_WINDOW <= receipt.date <= target.date
    before, after = BANK_WINDOW
    return target.date - before <= receipt.date <= target.date + after
