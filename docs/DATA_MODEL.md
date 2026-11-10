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
- Each owner's ordinals are unique, with one partial unique index per owner column: `(term_id, ordinal) WHERE term_id IS NOT NULL`, and the same for `invoice_fact_id` and `vendor_contact_id`. A plain `UNIQUE (term_id, invoice_fact_id, vendor_contact_id, ordinal)` would not work, because Postgres treats NULLs as distinct.
- `start_offset` and `end_offset` are null when the quote wasn't found in the document. CHECK that both are null, or both are set and `end_offset - start_offset = length(quote)`.

A term or an invoice fact can have several quotes, and a contact has exactly one. Extra quotes, such as invoice-date lines and notice addresses, come after the first.

### Citation offsets

- An offset is a Python `str` index (a code point), and the end is exclusive.
- Offsets index the raw stored file, headers included, decoded as UTF-8 with `errors="replace"` and **no newline translation**. A CRLF stays two characters. Use `leo.rules.read_source_text`. Don't use `Path.read_text()`, which turns CRLF into LF.
- These are the offsets `python3 scripts/validate_answer_key.py --offsets` prints.
- If a quote occurs more than once, the citation uses the first occurrence (`leo.rules.locate_quote`). If it doesn't occur, the item gets status `needs_review` (B4).
- Known limit: a quoted-printable or base64 body won't match the raw text, so its quotes go to `needs_review`. Every file in the synthetic dataset is plain text. A3 left this as it is: `leo.ingest.eml` decodes the body, but only as metadata, and stages still read the raw text. Fix it in 2.3 if real mail needs it.

## Source documents and raw files

Set by A3 (`leo/ingest/`). Every document is written by `leo.ingest.ingest`: uploads, the folder loaders and the seed.

- **Raw bytes** go to the raw store (`LEO_RAW_DIR`, default `var/raw`), under their content hash. `storage_path` is relative to that folder, and `content_sha256` is the hash of the bytes. Citation offsets index these stored bytes (`read_source_text`).
- **`path`** is the display path: dataset-relative for folder loads (`inbox/01_….eml`, `contracts/….md`), and `uploads/{filename}` for uploads. Like the other provenance columns, it keeps its first value, so D1's re-synced copy keeps the original's path.
- **`source_kind`** is `eml`, `file` or `attachment`.
- **Attachments.** Each attachment is its own row. Its key comes from its bytes, so it is the same row as that file uploaded on its own. Its `path` is `{email path}#attachments/{filename}`. `parent_document_id` is the first email that carried it, and every email carrying it gets a `document_link` with `link_type = "attachment"` (`document_id` is the email, `linked_document_id` the attachment).
- **Headers** (`sender`, `recipient`, `subject`, `message_id`, `received_at`) are decoded attributes and never part of a key. `received_at` is timezone-aware, or null when the `Date` is missing or malformed.

## Natural keys built from spans

The span is the first quote's (ordinal 0) `start:end`. When that quote isn't found in the document, the span is `q` plus the first 16 hex digits of the quote's SHA-256, for example `v1:term:{document key}:{term type}:q3f9a…`. The key still reproduces on a rerun, because B3's cache returns the same quote, and the item stays `needs_review`. `leo.rules.span_key` builds the span either way.

| Row | Natural key |
| --- | --- |
| Term | `v1:term:{document key}:{term type}:{start}:{end}` |
| Invoice fact | `v1:invoice-fact:{document key}:{start}:{end}` |

The answer key has no clashes under these keys: each `(document, type, span)` is unique across its terms, and each `(document, span)` across its invoice facts. `leo/db/keys.py` builds the key strings (A2).

## Deadlines

A finding's `deadline` is the contract date. The rule depends on its `deadline_basis`:

| Basis | Deadline | Function |
| --- | --- | --- |
| Notice window and renewal | `current_term_end` − `days`. When `relative_to` is `renewal_date`, count from `current_term_end` + 1 day. | `notice_deadline` |
| Dispute window and invoice fact | `invoice_date` + `days` | `dispute_deadline` |
| A single price term | the day before `effective_date` | `price_change_deadline` |

- `notice_deadline` accepts only notice windows `relative_to` `term_end` or `renewal_date`, and `dispute_deadline` only `invoice_date`. Any other value raises an error instead of returning a wrong date. A `price_change` notice window (such as Lumenpost's) has no deadline of its own: F2's deadline comes from the price term's `effective_date`.
- `recommended_send_by` is the deadline moved back to Friday when it falls on a Saturday or Sunday (`recommended_send_by`). Holidays are ignored.
- All deadlines are plain dates, with no time zones.
- A finding stores `deadline`, `recommended_send_by`, `deadline_notes`, `anchor_term_id` and a nullable `anchor_transaction_id`.
- The `finding_term` and `finding_invoice_fact` link tables have an `is_deadline_basis` boolean, so the deadline basis is a query.
- C1 computes the deadlines when it reconciles, and the repository stores them with the finding. The daily recheck ([should]) recomputes them.
