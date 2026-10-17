"""Proposals for unbooked transactions, with their evidence. Pure logic, no I/O."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from hamurai.models import Transaction
from hamurai.suggestions import Entry, Pair, Suggestions


class Confidence(StrEnum):
    CONFIDENT = "confident"
    UNCERTAIN = "uncertain"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Labels:
    """Display names: a category as code and name (codes repeat), a VAT code by description."""

    categories: Mapping[int, str]
    vat_codes: Mapping[int, str]

    def pair(self, pair: Pair) -> str:
        category = self.categories.get(pair.category_id, f"#{pair.category_id}")
        vat_code = self.vat_codes.get(pair.vat_code_id, f"#{pair.vat_code_id}")
        return f"{category} / {vat_code}"


class Proposal(BaseModel):
    transaction: Transaction
    confidence: Confidence
    category_id: int | None
    vat_code_id: int | None
    evidence: str


class ProposalSet(BaseModel):
    confident: list[Proposal] = []
    uncertain: list[Proposal] = []
    unknown: list[Proposal] = []
    skipped: list[Transaction] = []


def propose(transaction: Transaction, suggestions: Suggestions, labels: Labels) -> Proposal:
    """IBAN match → confident; name or description pattern → uncertain; else unknown."""
    account = transaction.counter_account
    if (entry := suggestions.by_iban(account.number)) is not None:
        return _from_entry(transaction, entry, Confidence.CONFIDENT, "IBAN match", labels)
    if (entry := suggestions.by_name(account.name_account_holder)) is not None:
        return _from_entry(transaction, entry, Confidence.UNCERTAIN, "Name match", labels)
    description = transaction.description.casefold()
    for pattern in suggestions.patterns:
        if pattern.contains.casefold() in description:
            return Proposal(
                transaction=transaction,
                confidence=Confidence.UNCERTAIN,
                category_id=pattern.category_id,
                vat_code_id=pattern.vat_code_id,
                evidence=f'Description contains "{pattern.contains}"',
            )
    return Proposal(
        transaction=transaction,
        confidence=Confidence.UNKNOWN,
        category_id=None,
        vat_code_id=None,
        evidence="No match",
    )


def propose_all(
    transactions: Sequence[Transaction], suggestions: Suggestions, labels: Labels
) -> ProposalSet:
    """Fully booked transactions are left out; partly booked ones are skipped, never proposed."""
    result = ProposalSet()
    for transaction in transactions:
        if transaction.is_fully_reconciled:
            continue
        if transaction.number_of_reconciliations > 0:
            result.skipped.append(transaction)
            continue
        proposal = propose(transaction, suggestions, labels)
        getattr(result, proposal.confidence.value).append(proposal)
    return result


def _from_entry(
    transaction: Transaction, entry: Entry, confidence: Confidence, match: str, labels: Labels
) -> Proposal:
    current = entry.current
    counts = entry.counts
    if entry.always_ask:
        history = "; ".join(
            f"{count}× {labels.pair(pair)}"
            for pair, count in sorted(counts.items(), key=lambda item: -item[1])
        )
        return Proposal(
            transaction=transaction,
            confidence=Confidence.UNCERTAIN,
            category_id=current.category_id,
            vat_code_id=current.vat_code_id,
            evidence=(
                f"{match}, always ask; last booked as {labels.pair(current)} "
                f"on {entry.bookings[-1].date}; history {history}"
            ),
        )
    older = sorted(
        ((count, pair) for pair, count in counts.items() if pair != current),
        key=lambda item: -item[0],
    )
    if older:
        before = "; ".join(f"{count}× {labels.pair(pair)}" for count, pair in older)
        evidence = (
            f"{match}, now {labels.pair(current)} ({counts[current]}×, since {entry.since}); "
            f"before that {before}"
        )
    else:
        evidence = (
            f"{match}, {counts[current]}× booked as {labels.pair(current)}, "
            f"last seen {entry.last_seen}"
        )
    return Proposal(
        transaction=transaction,
        confidence=confidence,
        category_id=current.category_id,
        vat_code_id=current.vat_code_id,
        evidence=evidence,
    )
