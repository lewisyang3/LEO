# Synthetic dataset: Brightwater Coffee Co. (v1)

A small, fully fictional inbox, contract folder and card ledger for a three-location coffee shop. It contains five planted issues, plus near-misses, dedup cases and noise. `expected.json` is the answer key.

All companies, people and domains are invented. Domains use the reserved `.example` TLD.

The answer key uses **schema v2** (Nov 2, 2026). The "v1" in the title and in the dataset ID `brightwater-v1` refers to the documents and transactions, which have not changed.

**Demo "today" is 2026-11-02**, with a 60-day lookahead (the same lead time as Ramp's renewal reminder). All deadlines and day counts in the answer key assume that date.

## Contents

| Path | What |
| --- | --- |
| `contracts/` | 6 agreements and order forms (plain text; PDFs can come later) |
| `inbox/` | 16 `.eml` files: price notices, invoices, an order confirmation, noise, a forward and a byte-identical re-sync |
| `transactions.csv` | 39 card charges using raw descriptors (`KESTREL*CLOUD 844-555-0142`, `PAYWAVE* LUMENPOST`). No vendor IDs, because matching is LEO's job. |
| `expected.json` | Answer key: document classes, vendors and aliases, 29 terms with source quotes, 6 invoice facts with invoice dates, vendor contacts, 5 findings (with anchor, deadline basis and recommended send-by date), 8 non-findings and 5 dedup checks |

Validate the key after any edit (this checks that every quote appears exactly once in its source, that all references resolve and that the finding arithmetic holds):

```
python3 scripts/validate_answer_key.py            # add --offsets to print quote_start/quote_end
python3 scripts/validate_answer_key.py --key PATH  # validate another copy of the key against these documents
```

## How the five issues were chosen

I chose the issues based on what spend-management vendors and contract benchmarks report as the most common leaks. Most of these sources are vendor reports or blogs rather than peer-reviewed studies, so treat the numbers as directional.

| # | Issue | Why it's common | Planted as |
| --- | --- | --- | --- |
| F1 | **Auto-renewal with a short notice window** | Auto-renewal clauses appear in 87% of cloud service agreements, and about 70% use a 30-day notice window (Common Paper, via CloudNuro). A Vertice benchmark puts it at about 70% of SaaS contracts. | Corvana CRM, $14,400/yr. Notice is due Nov 15 (a Sunday), 13 days out. The only Corvana email is an event invite. |
| F2 | **Price increase announced by email** | Median SaaS increases are about 8-12% a year, and a 2023 Vertice report found 73% of vendors raised prices. Many increases are disclosed only by notice. | Lumenpost Growth plan, $299 to $339 (+13.4%), from Dec 1. It is also forwarded internally, and the vendor's other plan is unchanged. |
| F3 | **Renewal uplift above a contractual cap** | Uncapped renewals rose about 8.4% in 2025, against negotiated caps of about 4-5%. Auto-renewed deals price higher than negotiated ones. | Kestrel hosting renewed at +9% against a 5% cap. $48/mo over, $96 so far. |
| F4 | **Billed seats above the commitment** | Unused or excess licenses are the most-cited SaaS waste. Reports range from 35% (Flexera) to about 50% (Productiv) of licenses unused. | Quillstack billed 32 seats on a 25-seat order form that requires a signed form to add seats. $126/mo over. |
| F5 | **Escalator applied early** | Practitioner write-ups describe escalators triggering months early, or not at all, because billing systems drift from contract terms. Billing errors are cited as about 38% of contract value leakage. | Halcyon applied its 3% annual increase in Oct 2026. The first anniversary is Jan 1, 2027. The invoice labels it "annual rate adjustment". |

Together these cover all three ways LEO catches a problem:
- **Dates alone:** F1.
- **Email alone:** F2.
- **Contract terms checked against charges:** F3, F4 and F5.

They also exercise each term type: renewal, notice window, price, price cap, seat count, escalator and dispute window.

### Near-misses (must NOT be flagged)

- **Ferrolink:** a 4% escalator applied correctly on its anniversary. This is the correct twin of F5.
- **Driftwell:** already renewed, with the next deadline outside the lookahead.
- **Lumenpost add-on:** same email as F2, but a different plan whose price is unchanged.
- **Halcyon:** 41 employees, under the 50 included.
- **Noise with prices in it:** a ZapLedger cold pitch, a Corvana event invite and a Lumenpost newsletter.

## Term value shapes

Every term's `value` has exactly the keys for its type. A key is `null` when the document doesn't say. The validator enforces this.

| Type | Keys | Enum values |
| --- | --- | --- |
| `price` | `amount`, `currency`, `per` | `per`: `year`, `month`, `seat_month` |
| `price_cap` | `max_increase_pct`, `applies_to`, `baseline` | |
| `escalator` | `pct`, `schedule`, `first_application` | |
| `renewal` | `auto_renew`, `initial_term_end`, `current_term_end`, `renewal_length_months`, `renewal_pricing`, `early_termination_fee` | |
| `notice_window` | `days`, `relative_to`, `applies_to`, `deliver_to`, `next_deadline` | `relative_to`: `term_end`, `renewal_date`, `price_change` |
| `seat_count` | `included`, `unit`, `overage_per_unit_month`, `increase_requires` | `unit`: `users`, `seats`, `employees` |
| `dispute_window` | `days`, `relative_to`, `applies_to`, `deliver_to` | `relative_to`: `invoice_date` |

`applies_to` is one of `non_renewal`, `vendor_price_change`, `invoice` or `renewal_term`. Fields beside `value`:
- `old_value` is in the `price` shape.
- `status` is absent (current) or `superseded`, with `superseded_by` pointing to the newer term and that term's `supersedes` pointing back.
- `notes` is free text.

Dispute windows have no `next_deadline`, because each invoice has its own deadline. A notice deadline counts back from `current_term_end`, or from the renewal date (`current_term_end` + 1 day) when `relative_to` is `renewal_date`.

Each finding records:
- its `anchor`, the term and transaction its natural key is built from
- its `deadline_basis`, the terms and invoice fact its deadline is computed from:
  - dispute window: invoice date + days
  - notice window: the rule above
  - single price term: the day before it takes effect
- its `recommended_send_by`, the deadline moved back to Friday if it falls on a weekend

## Citation locations

A citation location is a pair of character offsets into the raw stored file text, headers included. These are the offsets `--offsets` prints. A term's natural key uses the span of its first quote. Extra quotes, such as invoice-date lines and notice addresses, come after the first.

## Schema gaps (resolved in v2)

Writing the answer key surfaced gaps in the design doc's data model. Schema v2 resolves them:

1. **`price_cap`** is a term type (F3).
2. **Multiple citations per term.** Each term has a list of quotes.
3. **Invoice facts versus contract terms.** Invoice facts get their own table (see `ROADMAP.md` A1) and carry an `invoice_date`.
4. **`dispute_window`** is a term type (`kch_dispute`, `hps_dispute`), so the F3 and F5 deadlines follow from cited clauses.
5. **Weekend deadlines.** Findings keep the contract deadline and add `recommended_send_by` (F1: Sunday Nov 15, so send by Friday Nov 13).

## Sources

- [CloudNuro: SaaS statistics 2026 (Common Paper auto-renewal and notice-window figures)](https://www.cloudnuro.ai/blog/saas-statistics-2026)
- [Zylo: SaaS license utilization](https://zylo.com/blog/saas-license/)
- [Zylo: SaaS statistics](https://zylo.com/blog/saas-stats-it-strategy)
- [CloudEagle: ways you are overspending on SaaS](https://www.cloudeagle.ai/blogs/ways-you-are-overspending-on-saas)
- [Vertice: SaaS inflation, August 2023](https://www.vertice.one/insights/august-2023)
- [CFO Dive: SaaS prices jumped (Vertice)](https://www.cfodive.com/news/saas-prices-jumped-vertice-generativeai/691458/)
- [VendorBenchmark: price increase cap benchmarks](https://vendorbenchmark.com/blog/price-increase-cap-benchmark-negotiate)
- [Redress Compliance: SaaS price increase negotiation](https://redresscompliance.com/saas-price-increase-negotiation.html)
- [UpperEdge: hidden renewal clause](https://upperedge.com/contract-negotiations/hidden-renewal-clause-that-could-inflate-your-saas-costs/)
- [fynk: contract value leakage](https://fynk.com/en/blog/contract-value-leakage/)
- [ContractSafe: vendor contract audit](https://www.contractsafe.com/blog/vendor-contract-audit)
- [Orb: revenue leakage statistics](https://www.withorb.com/blog/revenue-leakage-statistics)
- [NSW Small Business Commissioner: subscription services warning](https://www.smallbusiness.nsw.gov.au/news/warning-about-subscription-services-small-businesses)
