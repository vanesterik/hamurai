# Payment-provider IBANs: design

Issue #2. It builds on the booking MCP (`docs/specs/2026-10-17-hamurai-booking-mcp-design.md`), whose constraints, anti-goals and matching rules apply here unless this document changes them.

## Intent

Payment providers collect for many merchants under one IBAN. The matcher looks up the IBAN first, so every merchant paying through, say, Adyen is proposed the category of whichever merchant was booked last through that IBAN. On the first live run (Q3 2026) two proposals came out wrong this way, one of them for a large amount.

For a transaction on a payment provider's IBAN, the tool matches and learns by the merchant's name instead, so one merchant's category is never proposed for another.

## What the data shows

A read-only look at the whole history on 2026-10-18 (3,401 transactions, 125 IBANs):

- **The merchant is in the counterparty name**, not only in the description: "Simon Levelt BV via Mollie", "Dropbox by Adyen", "NS Reizigers B.V. by Buckaroo", or just "Spotify AB" on an Adyen IBAN.
- **15 IBANs appear under more than one name**, but only some of them are providers. The others are spelling variants of one counterparty (Axus Nederland under 5 spellings, Talan Accountants, bol.com, the user's own accounts), for which matching by IBAN is right. Recognising providers automatically ("an IBAN with many names") would therefore misfire.

## Goals

- For a transaction on a provider IBAN, propose the merchant's latest choice, matched by counterparty name.
- Learn each such booking under the merchant's name, never under the provider's IBAN.
- Relearn the history already booked on provider IBANs, by merchant.

## Non-goals

- **Recognising providers automatically.** The list is kept by hand.
- **Merchants named only in the description**, such as Tikkie payment requests ("AAB Retail inz Tikkie", with "Wagamama" in the description). Tikkie stays an ordinary *always ask* IBAN.
- **Joining spellings of one merchant.** Names match exactly after the existing normalisation (case and spacing), so "Kaartje2go NL Verkoop via Stichting Mollie Payments" and "Kaartje2go via Mollie" would be two merchants. *(Builder's call.)*

## The provider list

`data/suggestions.yaml` gains a top-level list, kept by hand and normalised like any IBAN when it is read:

```yaml
provider_ibans:
- NL04ADYB2017400157   # Adyen
- NL51ABNA0565668625   # Adyen
- NL51DEUT0265262461   # Mollie
- NL70RABO0115600000   # Mollie
- NL56DEUT0265186420   # Buckaroo
- NL58CITI2032329913   # probably Stripe
- NL59ABNA0626226163   # Worldpay
```

These seven are the user's choice. Tikkie (NL13ABNA0506417344) and ING checkout (NL21INGB0674773837) stay off. *(The user's decision.)* `save` keeps the list, but comments written by hand are lost when the file is written, as they are now. *(Builder's call: noted in the README.)*

## Matching and learning

For a transaction whose counterparty IBAN is on the list:

- **Matching** skips the IBAN lookup. A name match is `uncertain`, as a name match on any bank transaction already is, so it's never in the confident table. *Always ask* on the merchant's entry still applies. Otherwise matching falls through to the description patterns, as now. *(The acceptance criteria of #2.)*
- **Learning** learns the booking under the counterparty name alone, as if the transaction had no IBAN. It joins an existing entry with that name under the existing rule, which also covers an entry with an IBAN of its own (the same merchant paid directly).
- **An entry whose own IBAN is on the list never answers a name lookup**, for matching or for learning. Until relearning removes them, the old provider entries carry whichever merchant's name was learned last, and would otherwise hand out their mixed history. *(Builder's call.)*
- **Seeding is not changed.** It runs once, before the suggestions file exists, so it can't know the list. A fresh setup seeds first, then lists the provider IBANs and runs `relearn_providers`. *(Builder's call: the README says so.)*

Transactions on IBANs not on the list are matched and learned exactly as now.

## Relearning the history

The seven IBANs' current entries mix merchants. Learned bookings don't record which merchant they were for, so the entries can't be split. Instead, the history is relearned by merchant. *(The user's decision.)*

A new tool, `relearn_providers()`:

1. Reads the provider list from the suggestions file. With an empty list, it refuses.
2. Fetches the whole transaction history, then the bookings of each fully booked transaction with one booking on a provider IBAN (about 55 read requests today), as seeding does.
3. Removes every entry whose IBAN is on the list, and takes back every booking it is about to relearn wherever it was learned before (for instance by `book` under the merchant's name, or by an earlier run). Then it learns each of those bookings again, oldest first, through the provider rule, and sorts every entry that received one by date, so its latest booking is its current choice. Where a merchant was booked in more than one way, the latest booking wins, as everywhere else. The old entries' *always ask* marks go with them. *(Builder's call: this keeps a second run from counting bookings twice, and keeps a merchant's earlier provider bookings from overriding its later direct ones.)*
4. Writes the suggestions file once, atomically, and returns a summary: per merchant, its name, how many bookings, and its current category and VAT code.

It writes only `data/suggestions.yaml`, never Hamurai. Running it again gives the same result, because of the take-back in step 3, so it is also how a newly listed IBAN is relearned. An expired session, or any read that fails, stops it before anything is written.

## Tools

| Tool | What changes |
|---|---|
| `propose_bookings`, `book` | Follow the provider rule above. Their parameters don't change. |
| `relearn_providers()` | New, as above. |

## Testing

As before, no test talks to Hamurai, and fixtures are invented (provider IBANs like `NL00PROV0000000001`).

| Unit | Key cases |
|---|---|
| `suggestions` | `provider_ibans` reads, normalises and survives a save; learning on a provider IBAN creates a name-only entry; two merchants on one provider IBAN become two entries. |
| `matcher` | A provider-IBAN transaction with a known merchant is `uncertain` with that merchant's latest pair, even when an entry with the provider's IBAN and the same name still exists; an unknown merchant falls through to the patterns; an IBAN not on the list is unchanged. |
| `server` | `relearn_providers` removes the provider entries, learns per merchant with the latest choice winning (also when the merchant already had a later direct booking), gives the same file when run twice, leaves entries that receive no relearned booking untouched, refuses with an empty list, and writes nothing when a read fails. |

**After building, the first real run:** list the seven IBANs, run `relearn_providers`, check the summary (Spotify AB on Adyen should come out with its 7 bookings), then ask for proposals for a quarter to see provider transactions proposed by merchant.

## Left to the builder

- Module layout and internal names.
- How the summary is formatted for Claude.
