"""Proposals for unbooked transactions, with their evidence. Pure logic, no I/O."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from pydantic import BaseModel

from hamurai.models import CounterAccount, Transaction
from hamurai.statement import StatementLine
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
    # Set when another party arrives on a learned IBAN; never changes the proposal itself.
    hint: str | None = None


class LineProposal(BaseModel):
    """A proposal for one line of a card statement."""

    line: StatementLine
    confidence: Confidence
    category_id: int | None
    vat_code_id: int | None
    evidence: str


@dataclass(frozen=True)
class _Choice:
    confidence: Confidence
    category_id: int | None
    vat_code_id: int | None
    evidence: str


NO_MATCH = _Choice(Confidence.UNKNOWN, None, None, "No match")


class ProposalSet(BaseModel):
    confident: list[Proposal] = []
    uncertain: list[Proposal] = []
    unknown: list[Proposal] = []
    skipped: list[Transaction] = []


def propose(transaction: Transaction, suggestions: Suggestions, labels: Labels) -> Proposal:
    """IBAN match → confident; name or description pattern → uncertain; else unknown."""
    choice = _match_counterparty(
        transaction.counter_account, transaction.description, suggestions, labels
    )
    return Proposal(
        transaction=transaction,
        confidence=choice.confidence,
        category_id=choice.category_id,
        vat_code_id=choice.vat_code_id,
        evidence=choice.evidence,
        hint=_provider_hint(transaction.counter_account, suggestions),
    )


def propose_line(line: StatementLine, suggestions: Suggestions, labels: Labels) -> LineProposal:
    """Merchant name match → confident (a card line has no IBAN); pattern → uncertain."""
    choice = _match_merchant(line.description, suggestions, labels)
    return LineProposal(
        line=line,
        confidence=choice.confidence,
        category_id=choice.category_id,
        vat_code_id=choice.vat_code_id,
        evidence=choice.evidence,
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


def _match_counterparty(
    account: CounterAccount, description: str, suggestions: Suggestions, labels: Labels
) -> _Choice:
    """A bank transaction's match: by IBAN, then by name, then by a description pattern. On a
    payment provider's IBAN, by the merchant's name only: the IBAN is shared by many."""
    if suggestions.is_provider(account.number):
        if (entry := suggestions.by_name(account.name_account_holder)) is not None:
            return _from_entry(
                entry, Confidence.UNCERTAIN, "Merchant match via payment provider", labels
            )
        return _from_pattern(description, suggestions)
    if (entry := suggestions.by_iban(account.number)) is not None:
        return _from_entry(entry, Confidence.CONFIDENT, "IBAN match", labels)
    if (entry := suggestions.by_name(account.name_account_holder)) is not None:
        return _from_entry(entry, Confidence.UNCERTAIN, "Name match", labels)
    return _from_pattern(description, suggestions)


# Legal forms: they say nothing about who the party is.
LEGAL_FORMS = frozenset({"ab", "bv", "gmbh", "inc", "limited", "ltd", "nv", "sa", "sl"})
# What follows these names the provider, not the party: "Dropbox by Adyen".
PROVIDER_WORDS = frozenset({"by", "via"})
# Below this similarity, two names with no word in common are taken as two parties.
SIMILAR = 0.5


def _provider_hint(account: CounterAccount, suggestions: Suggestions) -> str | None:
    """A hint when the name on a learned, unlisted IBAN looks like another party's."""
    if suggestions.is_provider(account.number):
        return None
    entry = suggestions.by_iban(account.number)
    if entry is None or not _other_party(account.name_account_holder, entry.name):
        return None
    return (
        f"{entry.iban} may be a payment provider: list it in provider_ibans and run "
        "relearn_providers."
    )


def _other_party(name: str | None, learned: str | None) -> bool:
    """No shared word of three letters or more, and little alike: one counterparty spelled
    several ways (Axus Nederland B.V., AXUS NEDERLAND N.V.) is not two."""
    words, learned_words = _words(name), _words(learned)
    if not words or not learned_words:
        return False
    if any(len(word) >= 3 for word in set(words) & set(learned_words)):
        return False
    return SequenceMatcher(None, " ".join(words), " ".join(learned_words)).ratio() < SIMILAR


def _words(name: str | None) -> list[str]:
    """The words naming the party: up to "by" or "via", without legal forms."""
    # Dots go first, so "B.V." becomes one word.
    words = [w for w in re.split(r"[^\w]+", (name or "").replace(".", "").casefold()) if w]
    cut = next((i for i, word in enumerate(words) if word in PROVIDER_WORDS), len(words))
    return [word for word in words[:cut] if word not in LEGAL_FORMS]


def _match_merchant(description: str, suggestions: Suggestions, labels: Labels) -> _Choice:
    """A card line's match: by the merchant's name, then by a description pattern."""
    if (entry := suggestions.by_name(description)) is not None:
        return _from_entry(entry, Confidence.CONFIDENT, "Merchant match", labels)
    return _from_pattern(description, suggestions)


def _from_pattern(description: str, suggestions: Suggestions) -> _Choice:
    folded = description.casefold()
    for pattern in suggestions.patterns:
        if pattern.contains.casefold() in folded:
            return _Choice(
                Confidence.UNCERTAIN,
                pattern.category_id,
                pattern.vat_code_id,
                f'Description contains "{pattern.contains}"',
            )
    return NO_MATCH


def _from_entry(entry: Entry, confidence: Confidence, match: str, labels: Labels) -> _Choice:
    current = entry.current
    counts = entry.counts
    if entry.always_ask:
        history = "; ".join(
            f"{_times(count)} as {labels.pair(pair)}"
            for pair, count in sorted(counts.items(), key=lambda item: -item[1])
        )
        return _Choice(
            Confidence.UNCERTAIN,
            current.category_id,
            current.vat_code_id,
            f"{match}, always ask; last booked as {labels.pair(current)} "
            f"on {entry.latest.date}; history: {history}",
        )
    older = sorted(
        ((count, pair) for pair, count in counts.items() if pair != current),
        key=lambda item: -item[0],
    )
    if older:
        before = "; ".join(f"{_times(count)} as {labels.pair(pair)}" for count, pair in older)
        # `since` starts the current run; the count covers every booking with the pair.
        evidence = (
            f"{match}, now {labels.pair(current)} since {entry.since} "
            f"(booked this way {_times(counts[current])} in all); before that {before}"
        )
    else:
        evidence = (
            f"{match}, booked {_times(counts[current])} as {labels.pair(current)}, "
            f"last on {entry.last_seen}"
        )
    return _Choice(confidence, current.category_id, current.vat_code_id, evidence)


def _times(count: int) -> str:
    return "1 time" if count == 1 else f"{count} times"
