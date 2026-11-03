# LEO Data-Model Rules

This page records the decisions from ROADMAP task 0.4. They settle the schema gaps the answer key surfaced (`data/synthetic/README.md`, "Schema gaps"). Track A builds the schema from them (A1, A2), Track B validates against them (B4), and Track C computes deadlines with them (C1).

The rules that can be code live in `leo/rules.py`, and `tests/test_rules.py` checks them against the answer key. To change a rule, edit this page and `leo/rules.py` in one PR that all three teammates approve.

## Term types

- There are seven term types: `price`, `price_cap`, `escalator`, `renewal`, `notice_window`, `seat_count` and `dispute_window`. Their value shapes are in the table in `data/synthetic/README.md` ("Term value shapes") and in `leo/types.py`.
- `contract_term.value` is JSONB in that shape. Validate it with `leo.types` before every write.
- `status` is `current`, `superseded` or `needs_review`. `superseded_by` and `supersedes` are foreign keys to `contract_term`.

## Invoice facts

Invoice facts get their own `invoice_fact` table, because they record what a vendor billed, not what was agreed. The columns follow `leo.types.InvoiceFact`:
- `vendor_id` and `document_id`
- `billing_period`, `invoice_date` and `amount`
- `invoice_number` and `seats_billed` (nullable)
- `matches_transaction`, a nullable foreign key to `transaction`
- `notes`

## Vendor contacts

Vendor contacts (`leo.types.Contact`) get a `vendor_contact` table with `vendor_id`, `email` and `purpose`, plus a citation for the quote they came from. It is unique on `(tenant_id, vendor_id, email, purpose)`. Like vendors, contacts get no hash key.

## Citations

Every quote is one `citation` row:
- `document_id`, `quote`, `start_offset` and `end_offset`
- `ordinal`, the quote's position in its owner's quote list. Ordinal 0 is the first quote.
- Its owner is exactly one of `term_id`, `invoice_fact_id` or `vendor_contact_id`, enforced with a CHECK.
- `UNIQUE (owner, ordinal)`.
- CHECK `end_offset - start_offset = length(quote)`.

A term, an invoice fact or a contact can have several quotes. Extra quotes, such as invoice-date lines and notice addresses, come after the first.

### Citation offsets

- An offset is a Python `str` index (a code point), and the end is exclusive.
- Offsets index the raw stored file, headers included, decoded as UTF-8 with `errors="replace"` and **no newline translation**. A CRLF stays two characters. Use `leo.rules.read_source_text`. Don't use `Path.read_text()`, which turns CRLF into LF.
- These are the offsets `python3 scripts/validate_answer_key.py --offsets` prints.
- If a quote occurs more than once, the citation uses the first occurrence (`leo.rules.locate_quote`). If it doesn't occur, the item gets status `needs_review` (B4).
- Known limit: a quoted-printable or base64 body won't match the raw text, so its quotes go to `needs_review`. Every file in the synthetic dataset is plain text. Revisit this in A3 if real mail needs it.

## Natural keys built from spans

The span is the first quote's (ordinal 0) `start:end`.

| Row | Natural key |
| --- | --- |
| Term | `v1:term:{document key}:{term type}:{start}:{end}` |
| Invoice fact | `v1:invoice-fact:{document key}:{start}:{end}` |

The answer key has no clashes under these keys: each `(document, type, span)` is unique across its terms, and each `(document, span)` across its invoice facts. A2 builds the key strings.

## Deadlines

A finding's `deadline` is the contract date. The rule depends on its `deadline_basis`:

| Basis | Deadline | Function |
| --- | --- | --- |
| Notice window and renewal | `current_term_end` − `days`. When `relative_to` is `renewal_date`, count from `current_term_end` + 1 day. | `notice_deadline` |
| Dispute window and invoice fact | `invoice_date` + `days` | `dispute_deadline` |
| A single price term | the day before `effective_date` | `price_change_deadline` |

- `recommended_send_by` is the deadline moved back to Friday when it falls on a Saturday or Sunday (`recommended_send_by`). Holidays are ignored.
- All deadlines are plain dates, with no time zones.
- A finding stores `deadline`, `recommended_send_by`, `deadline_notes`, `anchor_term_id` and a nullable `anchor_transaction_id`.
- The `finding_term` and `finding_invoice_fact` link tables have an `is_deadline_basis` boolean, so the deadline basis is a query.
- C1 stores the deadlines when it reconciles. The daily recheck ([should]) recomputes them.
