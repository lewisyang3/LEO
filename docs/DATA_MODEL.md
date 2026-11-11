# LEO Data-Model Rules

This page records the data-model decisions that settle the schema gaps the answer key surfaced (`data/synthetic/README.md`, "Schema gaps"). The schema is built from them, the validator checks against them, and the reconciler computes deadlines with them.

The rules that can be code live in `leo/rules.py`, and `tests/test_rules.py` checks them against the answer key. To change a rule, edit this page and `leo/rules.py` together.

## Term types

- There are seven term types: `price`, `price_cap`, `escalator`, `renewal`, `notice_window`, `seat_count` and `dispute_window`. Their value shapes are in the table in `data/synthetic/README.md` ("Term value shapes") and in `leo/types.py`.
- `contract_term.value` is JSONB in that shape. Validate it with `leo.types` before every write.
- `status` is `current`, `superseded` or `needs_review`. `superseded_by` and `supersedes` are foreign keys to `contract_term`.
- A fourth status, `rejected`, means a reviewer said the item isn't one (Phase 4, feedback loop). Invoice facts take `current`, `needs_review` or `rejected`. Everything that skips `needs_review` items (findings, plan matching, priors, price history) skips `rejected` ones too: `leo.types.EXCLUDED_STATUSES`.

## Invoice facts

Invoice facts get their own `invoice_fact` table, because they record what a vendor billed, not what was agreed. The columns follow `leo.types.InvoiceFact`:
- `vendor_id` and `document_id`
- `billing_period`, `invoice_date` and `amount`
- `invoice_number` and `seats_billed` (nullable)
- `matches_transaction`, a nullable foreign key to `transaction`
- `notes`

## Corrections (Phase 4, feedback loop)

A reviewer can correct a term's value, effective date, plan label or agreement ref, or an invoice fact's amount, period, date, number or seats, or reject either one (`leo.feedback`, `PATCH /api/terms/{id}`, `PATCH /api/invoice-facts/{id}` and their `/reject`). Type and quotes can't change, because they make the natural key.
- The row gets `corrected_at`. `Repo.upsert_term` and `upsert_invoice_fact` never overwrite a row that has it, and `write_term` / `write_invoice_fact` leave its citations alone, so re-extraction keeps the reviewer's version.
- A corrected `needs_review` item becomes `current`. An invoice fact can only become `current` with a billing period that parses; a rejected one may keep any period (`ck_invoice_period`).
- Each changed field writes one `feedback_event` (`event_type` `correct`, `field` such as `value.included`, `old_value`, `new_value`, `reviewer`). A rejection writes one with `event_type` `reject`. Every event records the item's `document_id` and the `engine` that extracted it (the extract job's `processed_by`), so it can be traced to a generated script.
- Either change asks for a reconcile round, so findings and unedited drafts follow on the worker's next pass.
- The events are also the extraction prompt's few-shot examples (F2, `leo.feedback.few_shots`), by item type, from the tenant's other documents.
- A correction on a generated script's output updates that `extractor_script` row's `accuracy` (documents it extracted with no reviewed item, over all it extracted) and, below 1.0, sets `demoted_at` (F3). A demoted latest version leaves its sender with no promoted script.

## Vendors and aliases

Vendors upsert on `normalized_name`, and each `vendor_alias` is unique per tenant on `normalized_alias`. Aliases grow as matches are made (`leo.match.vendors.DbVendorResolver`, A5): every name or card descriptor that resolves to a vendor is stored as an alias, so the next lookup is exact. `transaction.plan_label` and `invoice_fact.matches_transaction` are derived: only `leo.match.transactions` writes them, recomputed from the stored terms.

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
- **`path`** is the display path: dataset-relative for folder loads (`inbox/01_….eml`, `contracts/….md`), and `uploads/{first 8 hex of the SHA-256}-{filename}` for API uploads (C3), so two different files with one name don't share a path. `ingest()` called with no `path` still defaults to `uploads/{filename}`. Like the other provenance columns, it keeps its first value, so D1's re-synced copy keeps the original's path.
- **`source_kind`** is `eml`, `file`, `attachment` or `gmail` (A8).
- **Attachments.** Each attachment is its own row. Its key comes from its bytes, so it is the same row as that file uploaded on its own (except an attached email, which is stored re-serialized). Its `path` is `{email path}#attachments/{filename}`. `parent_document_id` is the first email that carried it, and every email carrying it gets a `document_link` with `link_type = "attachment"` (`document_id` is the email, `linked_document_id` the attachment).
- **Headers** (`sender`, `recipient`, `subject`, `message_id`, `received_at`) are decoded attributes and never part of a key. `received_at` is timezone-aware, or null when the `Date` is missing or malformed.

## Gmail accounts

Set by A8 (`leo/ingest/gmail.py`).

- **`connected_account`** is one synced mailbox: `provider` (`gmail`), `email_address`, and `history_id`, the sync cursor. It upserts on (tenant, provider, address), so a reconnect keeps its id.
- **Synced emails** key on `v1:gmail:{connected_account.id}:{gmail message id}`, never on the address or the bytes. Their `path` is `gmail/{message id}.eml`, their `source_kind` is `gmail`, and `thread_id` holds Gmail's `threadId`. Their attachments key on their bytes, as everywhere else. A synced email and the same email uploaded as an `.eml` are two rows.
- **`oauth_credential`** holds one account's token, encrypted with Fernet under `LEO_SECRET_KEY` (`leo/ingest/secrets.py`), with its scope (always `gmail.readonly`). It is kept apart from the extracted data: the read layer and the API never touch it.
- **Tenant.** The synced mailbox has its own tenant (`LEO_GMAIL_TENANT`, default `gmail-demo`), so it never doubles the seeded demo tenant's documents.

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
