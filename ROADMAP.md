# LEO Roadmap

How we get from the design doc (`LEO Design Doc.md`) to a working demo, and who builds what. Three people (**Lewis, Erik, Omar**) share the work. After a short shared start, Lewis and Omar build Track B together and finish it first. Owners for Tracks A and C are decided after that.

**The demo bar.** On the Brightwater synthetic dataset (`data/synthetic/`), LEO runs the full pipeline and:
- flags all five planted issues (F1–F5)
- flags none of the eight near-misses (N1–N8)
- passes the five dedup checks (D1–D5)
- cites the source text behind every number
- drafts a fix for each finding, which waits for human approval

"Today" is pinned to 2026-11-02 for the demo.

`data/synthetic/expected.json` is the answer key, and every track tests against it. Run `python3 scripts/validate_answer_key.py` after any edit to the dataset.

## How the work is split

| Track | Owner | Owns | Stages |
| --- | --- | --- | --- |
| **A. Data and platform** | _TBD (after B)_ | Postgres schema, keys and upserts, ingestion, job queue and worker, vendor and transaction matching, dedup | Storage, sync, stage plumbing |
| **B. AI pipeline** | Lewis and Omar (first) | Eval harness, classifier, LLM extraction and cache, validator, script extractors, action drafting, cost and funnel numbers | 1–4, drafting in 5 |
| **C. Findings and UI** | _TBD (after B)_ | Reconciliation rules, deadlines, API, review and approval UI, export | Reconcile in 5, the human gate |

### Working agreements

- **One branch per task**, named `<name>/<task-id>-<slug>` with the owner's first name in lowercase (for example `lewis/b1-eval-harness` or `erik/c1-reconcile`).
- **Each PR is reviewed by one teammate** before it merges.
- **Any change to `leo/types.py` or `leo/interfaces.py` needs all three to agree.**

**Order.** Track B comes first, built by Lewis and Omar together. Once it is done, we decide who takes Track A and who takes Track C.

**Now (2026-11-10).** 2.1 (wire the pipeline) is done: the worker takes an empty tenant to F1–F5 with drafts. Next is 2.2 (`make e2e`). A8's live Gmail run still waits on an OAuth client and a demo account. A8 (Gmail sync), A7 (seed and reset), A6 (duplicate linking), B5 (script extractors), A4 (job queue and worker, PR #17, replacing #13) and A5 (transactions, vendors and matching) are on main, and so are C3–C5 (PRs #14–#16).

The tracks don't block each other, whatever order they run in, because of two decisions made in Phase 0:

1. **Shared types first.** All three tracks code against one set of Python types (`leo/types.py`). Term values have one fixed shape per type, taken from the answer key.
2. **The answer key is a fixture.** A loader turns `expected.json` into those types with no field mapping.
   - Track B scores its output against the key.
   - Track C builds rules and UI on the key's correct terms before extraction or the database exist.
   - When Track A's repository lands, B and C swap the fixture for it behind the same interface.

```
Phase 0 (all) ──► Phase 1: B (Lewis + Omar) ──► A ─┬─► Phase 2: integrate (all) ──► Phase 3: demo (all) ──► Phase 4: stretch
                                                 C ─┘
```

Priority tags:
- **[must]** is needed for the demo bar.
- **[should]** makes the demo stronger.
- **[stretch]** waits until the demo is green.

---

## Phase 0: Foundations (all three, together)

Keep this short. Its job is to freeze the interfaces so the tracks don't block each other.

- [x] **0.0 [must] Answer key v2.** Make `expected.json` complete enough to build against:
  - dispute-window terms for Kestrel and Halcyon
  - `invoice_date` on every invoice fact, with a citation
  - one value shape per term type
  - finding `anchor`, `deadline_basis` and `recommended_send_by`
  - vendor `contacts`
  - Driftwell's notice deadline corrected to 2027-06-01

  The validator checks all of this, including the deadline arithmetic. The shapes are documented in `data/synthetic/README.md`.
- [x] **0.1 [must] Confirm the stack.** Confirmed: see `docs/STACK.md`.
  - Python 3.13 with uv, FastAPI, Pydantic v2, SQLAlchemy 2 and Alembic, PostgreSQL 16 with `pg_trgm` via Docker Compose, pytest and ruff.
  - Jev classifies items and compares new extractions with prior terms.
  - Claude Haiku 5.5 extracts terms and drafts emails. A job moves to a heavier model only on eval evidence.
  - Frontend: React + Vite + TypeScript.
- [x] **0.2 [must] Repo skeleton.**
  - Package layout: `leo/` with `db/`, `ingest/`, `pipeline/`, `reconcile/`, `actions/` and `api/`, plus `web/` for the frontend.
  - `docker-compose.yml`, and `.env.example` with `ANTHROPIC_API_KEY`, `JEV_API_KEY`, `LEO_MODEL_EXTRACT`, `LEO_MODEL_DRAFT`, the database URL and `LEO_TODAY`.
  - Make targets or scripts for `up`, `migrate`, `seed`, `test`, `eval` and `e2e`.
  - A README quick-start.
- [x] **0.3 [must] Shared types** in `leo/types.py` (Pydantic), copied from the answer key's shapes:
  - **Term value models.** One per type: `price`, `price_cap`, `escalator`, `renewal`, `notice_window`, `seat_count`, `dispute_window`.
  - **`ExtractedTerm`.** Fields: `vendor`, `document`, `type`, `value`, `quotes` (a list), `plan_label`, `agreement_ref`, `effective_date`, `status`, `superseded_by`, `supersedes`, `old_value` (in the price shape) and `notes`.
  - **Records:** `Citation`, `SourceDocument` (with `DocumentClass`), `InvoiceFact` (with `invoice_date`), `Vendor` (with `contacts`) and `Transaction`.
  - **`Finding`.** Many terms, transactions and invoice facts, plus `anchor`, `deadline`, `deadline_basis`, `recommended_send_by` and `impact`. `kind` is an enum of the key's five strings: `auto_renewal_deadline`, `price_increase_notice`, `price_cap_exceeded`, `seats_over_commitment`, `escalator_applied_early`.
  - **`ActionDraft`.** Its type is one of `cancellation_notice`, `renegotiation_request`, `negotiation_email` or `billing_dispute`. It also has `to`, `pre_step` and `alternative`.
- [x] **0.4 [must] Data-model rules.** Recorded in `docs/DATA_MODEL.md`, with the rules as code in `leo/rules.py`. These settle the schema gaps from `data/synthetic/README.md`:
  - `price_cap` and `dispute_window` are term types (see 0.0).
  - A term has a list of citations.
  - Invoice facts get their own table, because they record what was billed, not what was agreed.
  - A deadline shows the contract date, plus `recommended_send_by` (moved back to Friday when the deadline falls on a weekend).
  - **Citation location** is character offsets into the raw stored file text, the same offsets `validate_answer_key.py --offsets` prints. A term's natural key uses the span of its first quote.
- [x] **0.5 [must] Interfaces between tracks.** Signatures and stubs for `Classifier`, `Extractor`, `Validator`, `TransactionSource`, `VendorResolver`, `Reconciler` and `Drafter`, plus a `Clock` that returns `LEO_TODAY`. The protocols, their input and output models and the contracts between tracks are in `leo/interfaces.py`, the stubs in `leo/stubs.py` and the clock in `leo/clock.py`.
- [x] **0.6 [must] Fixture loader** `leo/fixtures.py`. It reads `expected.json`, `transactions.csv` and the documents straight into the shared types. Owner: Lewis (done).
  - `load_fixture()` returns a `Fixture` with every key section, `ledger()`, `document_text(path)` and `clock()`. The key-only finding fields (`expected_action`, `days_until_deadline`, `why_planted`) are split into `ExpectedFinding`.
  - Fixture-backed stages (`FixtureClassifier`, `FixtureExtractor`, `FixtureVendorResolver`, `FixtureReconciler`, `FixtureDrafter`) return the key's answers through the 0.5 interfaces. Use `StaticTransactionSource(fixture.transactions)` for transactions.
- [x] **0.7 [must] Owners and working agreements.** Lewis and Omar build Track B together first. Owners for Tracks A and C are decided once B is done. See the track table, "Working agreements" under "How the work is split", and the owners in the open-questions table.

**Exit:** `make up && make test` runs on all three laptops, and the stubs and fixture loader are merged.

---

## Phase 1: Parallel build

Track B goes first. Each track's tasks are in order. Items marked ⇄ are handoff points another track waits on.

### Track A: Data and platform (owner TBD after B)

- [x] **A1 [must] Schema and migrations.**
  - The first Alembic migration creates:
    - every table in the design doc: `tenant`, `vendor`, `vendor_alias`, `source_document`, `contract_term`, `citation`, `transaction`, `finding`, `action`, `extractor_script`, `feedback_event` and `job`
    - four additions: `invoice_fact`, `vendor_contact`, `document_link` for duplicate links, and `finding_term` / `finding_transaction` / `finding_invoice_fact` for linking a finding to many items
  - Every table has `tenant_id`, a UUIDv7 primary key, and `UNIQUE (tenant_id, natural_key)` where it has a natural key.
  - Raw model output goes in JSONB.
  - Enable `pg_trgm`.

  Done, in `leo/db/models.py` and migration `7f576f9fa5fc` (PR #10):
  - **Tenant isolation.** `tenant_id` is a foreign key to `tenant`, and every table has `UNIQUE (tenant_id, id)`. Every cross-table reference is a composite foreign key on `(tenant_id, col)`, so a row can't point at another tenant's rows. Self-references (`supersedes`/`superseded_by`, `parent_document_id`, `*_duplicate_of_id`) are `DEFERRABLE INITIALLY DEFERRED`, so terms that supersede each other can be written in either order.
  - **CHECKs.** Their value lists come from the `leo.types` enums. Finding status is `current` or `superseded`, and job status is `pending`, `running`, `done`, `failed` or `parked`. An `approved` or `sent` action must have an `approver`.
  - **Design-doc columns.** `action.approver`/`decided_at`, `extractor_script.code`/`accuracy`/`promoted_at`, the correction fields on `feedback_event`, and `job.claimed_at`/`updated_at` with a claim index.
  - **Migrations.** `make migration msg=…` autogenerates one. Add extensions by hand, because autogenerate can't emit them. `leo/db/migrations/versions/` is exempt from ruff's line-length rule, for the generated CHECK SQL.
  - **Duplicates.** `document_link` is the one representation. A6 dropped the `*_duplicate_of_id` columns.
- [x] **A2 [must] Keys and upserts.**
  - Natural-key builders: `v1:gmail:…`, `v1:eml-hash:…`, `v1:file:…`, `v1:term:…`, `v1:invoice-fact:{doc key}:{first quote start}:{first quote end}`, `v1:finding:{anchor term key}:{anchor txn key or -}:{kind}`, `v1:txn:{source}:{external_id}`, `v1:job:{doc key}:{stage}` and `v1:action:{finding key}:{action type}`.
  - A repository layer where every write is an upsert.
  - Tests:
    - Running the seed twice gives the same rows and IDs.
    - D1 (the byte-identical re-sync) gives one document row.
    - D5 (a full rerun) adds no rows to any table.
  - ⇄ B and C switch from fixtures to the repository once this lands.

  Done, in `leo/db/keys.py`, `leo/db/repo.py`, `leo/db/seed.py` and migration `9c5628160120` (PR #11):
  - **Keys.** `leo/db/keys.py` builds every `v1:` key. `.eml` files key on `eml-hash` and all other files on `file`, so contracts get `v1:file:…`. Transactions use `v1:txn:csv:{id}`. `normalize_name` gives `vendor.normalized_name` and `vendor_alias.normalized_alias`.
  - **Upserts.** `Repo(session, tenant_id)` never commits. Every write is `INSERT … ON CONFLICT DO UPDATE … RETURNING id`, so a rerun gets back the same id. On conflict:
    - provenance columns (path, hash, headers) keep their first value
    - derived values are overwritten
    - `action.state`, `approver` and `decided_at` are never written, and draft text updates only while the action is still a draft, so approvals survive reruns
    - citations upsert by (owner, ordinal) and drop stale ordinals
    - an alias that already names another vendor raises `AliasConflict`
  - **Tenants.** `tenant.slug` is the tenant's natural key. The seed's slug is the dataset name.
  - **Seed.** `seed(session)` loads the answer key under one tenant: 223 rows. Vendors, transaction mappings and the D2 link (`document_link`, `probable_duplicate`) came straight from the key. A5 replaced the vendors and mappings with matching, and A6 replaced the D2 link with a computed one. A byte-identical copy (D1) reuses the original's row and doesn't overwrite its notes. `make seed` runs it (C3), and `make reset` wipes the schema and reseeds (A7).
  - **Not done here.** `.eml` header decoding with `email.policy.default` and a guarded date parse belong to A3 (done there). The read methods B and C need to switch over belong to C3.
- [x] **A3 [must] Ingestion function.**
  - An `.eml` parser covering headers, text body, and attachments stored once by hash.
  - A contracts-folder loader.
  - `ingest(file)` for the upload endpoint (C3) to call.
  - Raw files go in a local folder, and the database stores references.
  - `Message-ID`, `threadId` and similar headers are attributes, never part of a key.

  Done, in `leo/ingest/store.py`, `leo/ingest/eml.py` and `leo/ingest/ingest.py`:
  - **Raw store.** `RawStore` saves each file under its content hash, at `{sha[:2]}/{sha}{suffix}` in `LEO_RAW_DIR` (default `var/raw`, gitignored). Writes are atomic, and files are `0644`. One folder serves every tenant: the same bytes are written once, and tenant isolation lives in the rows. A row holds `storage_path` and `content_sha256`.
  - **Parser.** `parse_eml` decodes headers with `email.policy.default`. A missing or malformed `Date` gives `None`, and a date with no zone is taken as UTC. The body is the plain-text part, or the HTML part with its tags removed. Body and headers are metadata only: the stages still read the raw stored text, which citation offsets index.
    - Attachments are found at any depth (a signed invoice nests them), and a single-part email whose only part is an attachment gives that attachment. A text part with no filename and no `attachment` disposition counts as part of the body, not as an attachment.
    - Attachments keep their original bytes (`get_payload(decode=True)`), so an attached contract hashes the same as the file on its own.
    - An attached email (`message/rfc822`) is kept as `attached-{n}.eml`. A part with no filename gets a generated one, and a part is skipped only when it has no content.
    - NUL is stripped from headers and filenames, because Postgres `text` columns reject it.
  - **`ingest(repo, store, raw, filename, path=None)`.** Keys the document on its bytes with A2's builders and returns the id, key, whether the row is new, and the attachment ids. It never commits. `path` defaults to `uploads/{filename}`.
    - Each attachment is its own `source_document` (`source_kind="attachment"`, path `{email path}#attachments/{name}`). Bytes already stored, for example as a contract file, reuse that row.
    - `parent_document_id` is the first email that carried an attachment, and is now insert-only. Every carrier gets a `document_link` of type `attachment`.
  - **Loaders.** `ingest_folder` reads one folder in name order, skipping dotfiles, READMEs and subfolders, and stores `{folder}/{name}` paths. `ingest_folder(…, "contracts")` is the contracts loader, and `ingest_dataset` reads `inbox/` then `contracts/`.
  - **Seed.** `seed()` now goes through `ingest`, so it stores real raw copies (still 223 rows). `Repo.update_document` sets the key's class, vendor and notes once per row, so D1's copy still doesn't overwrite the original's.
  - **Tests.** Every test's `LEO_RAW_DIR` points at a temp folder (`tests/conftest.py`).
  - **Not done here.** Queueing jobs on ingest (A4), and reading the text of binary attachments such as PDFs. Those are stored but not converted to text, and the dataset has none. Attached emails aren't parsed for their own headers or attachments. An attached email is stored re-serialized, so its bytes, and its key, differ from the same email loaded on its own. A6's fingerprint gives the two the same value, but A6 links them only when one reads as a forward, so they usually stay unlinked.
- [x] **A4 [must] Job queue and worker.**
  - Jobs are rows claimed with `FOR UPDATE SKIP LOCKED`.
  - The worker runs each item through the stage interfaces in `leo/interfaces.py`, using `leo/stubs.py` until B and C plug in.
  - It records which stage handled each item (`processed_by`) and retries or parks failures.

  Done, in `leo/worker/` and migration `06c67d5193ee`. This replaces PR #13, which wrote job statuses the CHECK rejects and called none of the interfaces. Its claim query and bounded retries carried over.
  - **Jobs.** Each document gets one row per stage, keyed `v1:job:{doc key}:{stage}`. The stages are `classify -> extract -> validate -> reconcile`, and noise stops after classify. Finishing a stage queues the next one in the same transaction. `reconcile` reconciles the tenant's whole ledger and drafts each finding, so it runs once per document. That's fine at demo scale.
  - **Queue** (`queue.py`).
    - `enqueue` is `ON CONFLICT DO NOTHING`, so re-ingesting requeues nothing. It deliberately avoids `Repo.upsert_job`, which updates.
    - `claim` takes the oldest due `pending` job with `SKIP LOCKED`. It sets `running` and `claimed_at`, and counts the attempt. It and `drain` take an optional tenant filter, which the tests use.
    - `recover_stale` handles jobs left `running` for more than 10 minutes. It parks the ones out of attempts, so a stage that kills the worker every time stops being retried, and resets the rest. It runs when the worker starts and after every idle poll.
  - **Ingest.** `ingest()` queues a `classify` job for each document, attachments included. The seed passes `enqueue=False`, because it writes the key's results itself.
  - **Stages** (`run.py`).
    - Each stage reads the previous stage's output from that job's `raw_model_output`. `doc_class` comes from the classify job's output.
    - Extractors are tried in order, and the first non-None result wins. If every extractor returns None, the job is `failed`.
    - Validate follows the interfaces' vendor order (find, prior terms, validate, create on `vendor_ok`). When `vendor_ok` is False it fails closed.
    - `processed_by` is the result's `engine`. Stubs record `stub`.
    - The components live in `Pipeline` (`pipeline.py`), and `Pipeline.stubs()` is the default.
  - **Failures.** `LLMOfflineMiss`, a missing raw file, output that doesn't parse, and "no extractor matched" are not retried: the job is `failed`. Any other error retries after `run_after` (2s, then 4s), so other jobs keep running meanwhile. After 3 attempts the job is `parked`, so a failed classification stays unclassified, never noise. On Ctrl-C or SIGTERM, the worker hands its job back to `pending` without using up an attempt.
  - **Run.** `make worker` polls. `python -m leo.worker --drain` stops when nothing is due.
  - **Not done here (2.1).** A4 writes only `job` rows: no document class, terms, vendors, findings or actions. The real B and C components, prior terms read from the store, and writes through `Repo` in the stage's transaction are all 2.1.
- [x] **A5 [must] Transactions, vendors and matching.**
  - A `TransactionSource` that reads `transactions.csv`, the same interface a Ramp connection would use later.
  - Vendors come from the data, never from the answer key:
    - created from extracted non-noise documents, once the vendor name passes validation, and from normalized transaction descriptors that match nothing
    - aliases grow as matches are made
  - Matching:
    - normalize descriptors by stripping processor prefixes like `PAYWAVE*`, phone numbers and store numbers
    - then look up aliases
    - then use `pg_trgm` similarity
  - Also match transactions to plans, by amount and the current price term, and invoice facts to transactions.
  - Tests, using the key only to check results:
    - every transaction resolves to its vendor in `transaction_vendor_map`
    - plans match `transaction_plan_map`
    - invoice facts match `matches_transaction`
    - ZapLedger never becomes a vendor
    - Sunridge and Metro become vendors from their transactions alone

  Done, in `leo/match/`:
  - **Names** (`names.py`). `clean_descriptor` drops a known processor prefix (`PAYWAVE*`, `SQ *`, `TST*`, `PAYPAL *`, `SP *`), turns any other `*` into a space (`KESTREL*CLOUD`), and drops phone and store numbers. `match_form` is `normalize_name` without trailing legal suffixes (`inc`, `llc`, ...). A descriptor that matches nothing names its vendor in title case (`Metro Water & Power`).
  - **Resolver** (`vendors.py`). `DbVendorResolver(repo)` implements `VendorResolver`. Lookup is an exact match on the normalized name or an alias, then a fuzzy match in two steps. `pg_trgm` gives candidates (`greatest(similarity, word_similarity)` on the suffix-stripped forms, at least `0.3`, best first). The first candidate whose words agree (`words_agree`: the first words match, and every word of the shorter name matches one of the longer, by equality, prefix or abbreviation such as `payrl` for `payroll`) wins. `word_similarity` alone merges names that share one word (`metro cafe` and `metro water and power` score 0.55), so the word check is what decides (A5 review). Every name or descriptor that resolves becomes an alias. `find_vendor` never writes.
  - **Transactions** (`transactions.py`). `CsvTransactionSource` reads the CSV. `sync_transactions` upserts each charge with `vendor_for_descriptor`. `match_plans` labels a charge only for a vendor with two or more plans, when exactly one plan's price in force on the charge date equals the amount. `match_invoice_facts` takes the nearest charge of the same vendor and amount within 7 days, each charge at most once. Those two are the only writers of `plan_label` and `matches_transaction`.
  - **Seed.** Vendors now come from each document's vendor name (as `FixtureExtractor` reports it) through the resolver, non-noise first. Noise only looks up, so ZapLedger never becomes a vendor. The key's aliases and vendor notes are no longer written. Transactions come from the CSV, and plans and invoice links are matched. The key is only used to check results (`tests/test_match.py`), including as-written names from the extract cache resolved names-first and descriptors-first.
  - **Not done here (2.1).** The worker still uses `StubVendorResolver`; 2.1 swaps in `DbVendorResolver` and runs `sync_transactions`, `match_plans` and `match_invoice_facts` before reconcile.
- [x] **A6 [must] Duplicate linking.**
  - Compute `context_fingerprint` and suggest links per the design doc's table, linking on the fingerprint alone plus a forwarded-message check (`Fwd:` subject, forwarded header block), because the dataset has no attachments.
  - Build and test it on the fixture's terms first, so it doesn't wait on B.
  - Terms from a linked duplicate are not counted again.
  - `document_link` also holds A3's `attachment` links (email -> attachment). Filter on `link_type`, and use them for the design doc's "same attachment hash" signal.
  - Tests:
    - D2: the forward is linked.
    - D3: monthly invoices stay separate.
    - D4: a two-plan vendor keeps separate price histories.

  Done, in `leo/match/duplicates.py` and migration `ce73cab1a6ed`:
  - **Fingerprint.** `context_fingerprint` is `v1:` plus a SHA-256 of a document's stored items:
    - terms as (vendor, type, value, plan, agreement, effective date)
    - invoice facts as (vendor, invoice number, billing period, invoice date, amount)

    Quotes, offsets, ids, status and notes are left out. A document with no items has no fingerprint. It is stored on `source_document.context_fingerprint` and on the link row.
  - **Links.** `link_duplicates(repo, store)` groups documents by fingerprint, and the original is the earliest document in the group that isn't a forward. A forward (`Fwd:`/`Fw:` subject, or a `Forwarded message`/`Original Message` block) links to it as `probable_duplicate`. A non-forward links only if the two emails carry the same attachment (A3's `attachment` links). Everything else stays separate. The links are recomputed each run: stale `probable_duplicate` links are deleted (`Repo.prune_links`).
  - **Counted once.** `load_ledger` leaves out a linked duplicate's terms and invoice facts, as `leo/interfaces.py` requires. `load_price_history` already did.
  - **Seed.** The forward (02) now stores its own copy of 01's terms, cited in its own text, as an extractor would give it (contacts aren't copied). `link_duplicates` runs after matching, and the key's link is no longer copied.
  - **Tests** (`tests/test_duplicates.py`): D2, D3 and D4, the attachment signal, rerun stability, and stale-link removal.
  - **Not done here.** Confirming or rejecting a link in the UI, with its feedback event. Wiring into the worker is left to 2.1, which runs `link_duplicates` after storing terms, next to `match_plans`.
    - 2.1 follow-ups from review:
      - The forward check reads the raw stored text. A base64 or quoted-printable body with no `Fwd:` subject isn't recognized, so decode the body first.
      - Term values are hashed as stored. Normalize numbers in the value JSON (`339` and `339.0`) before fingerprinting, so two extractors' copies agree.

- [x] **A7 [should] Seed and reset.** `make seed` loads the synthetic tenant, and `make reset` reruns migrations and seed. Deterministic keys make this safe.
  - `make seed` was built in C3 (`python -m leo.db.seed`).

  Done, in `leo/db/reset.py`:
  - **Reset.** `make reset` drops and recreates the `public` schema, runs `alembic upgrade head`, then seeds, all in one process. It drops the schema rather than running `alembic downgrade base`, so it doesn't depend on any downgrade working. The init migration recreates `pg_trgm`. A reseed keeps approvals and edits, but a reset brings every action back as an unapproved draft.
  - **Guards.** It refuses a database whose host isn't `localhost`, loopback or `db` unless `--force` is passed, and it prints the URL with the password hidden. The drop waits at most 5s for locks (`lock_timeout`). If the API or worker is mid-transaction, it stops and says to stop them first.
  - **Not done here.** `LEO_RAW_DIR` isn't emptied: it's content-addressed, and the seed rewrites the same files. Uploaded bytes stay there with no row pointing to them, which does no harm.
    - Small follow-ups: `Config("alembic.ini")` is relative, so it only works from the repo root (as `make` runs it). A Unix-socket `DATABASE_URL` (no host) counts as non-local and needs `--force`.
  - **Tests** (`tests/test_reset.py`): the local-host check, and the refusal of a remote database without dropping anything. No test drops the dev schema, because `make test` shares it. The reset itself was checked by hand: two runs in a row give the same row counts, and an approval made before a reset is back to a draft afterwards.
- [x] **A8 [should] Gmail sync.**
  - Read-only OAuth into a seeded demo Gmail account.
  - Incremental sync by `historyId`.
  - Tokens encrypted at rest.
  - The `.eml` path stays as the fallback, so the demo never depends on this.

  Done, in `leo/ingest/gmail.py`, `leo/ingest/secrets.py`, `scripts/seed_gmail.py` and migration `0f19fbbdb783`:
  - **Accounts.** `connected_account` upserts on (tenant, provider, address), so a reconnect keeps its id. Its `history_id` is the sync cursor. `oauth_credential` holds the token, encrypted with Fernet under `LEO_SECRET_KEY`, in its own table: the read layer and the API never touch it.
  - **Tenant.** The mailbox gets its own tenant (`gmail-demo`). It holds the same emails the seed loads, under different keys, so syncing into `brightwater-v1` would double every document. `make api-gmail` serves it.
  - **Sync.** `GmailClient` is a Protocol, like `TransactionSource`, with `GoogleGmailClient` over the Gmail API. The first sync, or a sync whose cursor has expired (404), lists the inbox, reading the profile's `historyId` first. After that, it reads `history.list` (`messageAdded`, `INBOX`). Each new message goes through `ingest()` with the key `v1:gmail:{account id}:{message id}`, the path `gmail/{id}.eml`, `source_kind` `gmail` and `thread_id`, which queues `classify`. A message already stored is skipped without being fetched. A message deleted before the fetch (404) is skipped and counted, so it can't stall the cursor. Documents and the cursor commit together, one account at a time. An account that fails is rolled back, and the rest still sync. An expired access token is refreshed and stored again, encrypted.
  - **Commands.** `make gmail-connect` (consent in the browser, read-only), `make gmail-sync` (`ARGS="--watch 10"` polls), and `make gmail-import` (`scripts/seed_gmail.py`). The import uses `messages.import` with the dates taken from the headers, and offers `--drip SECONDS` and `--only FILE`. Its `gmail.insert` token is never stored. It isn't idempotent.
  - **Tests** (`tests/test_gmail.py`, with a `FakeGmail`):
    - full and incremental sync, and an unchanged rerun that makes no fetches
    - expired history, and a deleted message
    - a reconnect keeps the account id
    - the token is encrypted at rest, and a missing key explains how to make one
    - a synced email and its upload are separate rows
    - `sync_all` keeps going past a failing account
  - **Not done here.**
    - The live run against a real account waits on a Google Cloud OAuth client and a demo Gmail account (README, "Gmail sync"): connect, import, a sync giving 16 then 0 new, a `--watch` pickup of one `--only` import, and a check that the stored ciphertext has no readable token.
    - A message that gets the `INBOX` label after it arrives (`labelAdded`) isn't picked up.
    - 2.1 turns the queued jobs into terms.
    - A larger dataset belongs to its own task, because every new email changes the answer key. `seed_gmail.py --folder` takes any folder.

### Track B: AI pipeline (stages 1–4 and drafting) (Lewis and Omar, first)

- [x] **B1 [must] Eval harness first.** `make eval` scores against `expected.json`:
  - classification accuracy, and recall on the documents that matter
  - term precision and recall on vendor, type, value, effective date and plan
  - whether each quote matches, using `leo.rules.locate_quote` (it agrees with `find_quote` in `scripts/validate_answer_key.py`)

  It prints a per-document report. This is how B knows when it's done. Done, in `leo/eval/`:
  - `python -m leo.eval --classifier NAME --extractor NAME [--doc PATH ...] [--json PATH]`. Engines are registered by name in `leo/eval/runner.py`: `fixture` (the reference, scores 100%) and `stub` for now. B2 and B3 add theirs there.
  - Classification and extraction are scored independently. Extraction runs on every document whose key class isn't noise.
  - Terms, invoice facts and contacts each get precision and recall, field accuracy (each value key separately), whether every quote is in the document, and whether the first quote overlaps the key's. It also reports whether every term and invoice fact behind F1–F5 was extracted.
  - **Duplicate rule:** a duplicate document (02, 07) is scored against its original's items, because the extractor reads what's in the document and linking duplicates is A6's job.
- [x] **B2 [must] Classifier (stage 1).**
  - **[must]:** a `Classifier` interface with Jev (TypeSafe AI) as the classifier. There is no LLM step.
  - **[must]:** Jev's P(noise) alone decides noise. Below the threshold, a document is kept with its most probable non-noise class.
  - **[must]:** a Jev error is raised and the document stays unclassified for review. It never becomes noise.
  - Build a labeled set from the 22 documents (the answer key's classes).
  - Choose the "noise" threshold that keeps 100% recall on the planted issues, and report the false-pass rate.
  - Keep Jev's probabilities on every label, and list the noise-labeled documents in the eval for a spot check. This replaces the LLM tripwire.

  Done, in `leo/pipeline/classify.py` (`JevClassifier`), built from Omar's prototype (PR #4), with the threshold sweep in `leo/eval/scoring.py`:
  - **One Jev call per document,** a choice over the six `DocumentClass` labels, with the raw stored text (`DocumentText.text`, the same text extraction sees) as state. The client is `leo.jev.jev_client`.
  - **Noise rule:** noise when P(noise) ≥ `NOISE_THRESHOLD` (0.5). Otherwise, the most probable non-noise class.
  - **Errors:** a Jev error or an unusable answer raises `ClassificationFailed`, which the eval records as an error (never noise). A missing `JEV_API_KEY` stops the run at startup.
  - **Evidence:** `Classification.reason` holds Jev's probabilities. The eval prints P(noise) per document, the noise-labeled documents and a threshold sweep with the lowest threshold that keeps full recall.
  - **Eval** (`uv run python -m leo.eval --classifier jev`, 2026-11-05):
    - recall on the documents that matter: 9/9
    - noise false-pass: 0/5
    - accuracy: 21/22
    - Every document that matters scored P(noise) ≈ 0.00. The five noise documents scored 0.84–1.00, so any threshold from 0.5 to 0.84 gives the same result.
    - The one miss is 08 (Quillstack), which the key calls an invoice and Jev calls a receipt (0.94). The email calls itself a receipt for a card charge. Noise isn't affected.
    - There is no Jev cache. A run is 22 calls.
  - **Held-out eval** (`uv run python -m leo.eval.holdout`, 2026-11-05, prompt and threshold unchanged): 5 documents written after tuning, in `data/holdout/` (labels in `labels.json`, result in `results/jev-2026-11-05.json`). "Matters" there means not noise. With n=5 these numbers are directional only.
    - accuracy: 4/5
    - recall on non-noise documents: 3/3. The price increase buried in a newsletter (h1) was caught at P(noise) 0.00.
    - noise false-pass: 1/2. h4, a Pinegrove product-launch email quoting prices for an add-on and a plan the customer doesn't have, scored P(noise) 0.35 and was kept as a price notice. The internal budget email (h5) scored 0.86 and was dropped.
    - 0.5 still keeps every non-noise document, which scored 0.00. Catching h4 would take a threshold at or below 0.35, which no tuned-set document that matters comes near, but changing it on this evidence would be tuning on the holdout. A vendor marketing email that quotes prices is the classifier's weak spot. The cost is a false pass, which only sends a noise document on to extraction.
- [x] **B3 [must] LLM extraction (stage 3).**
  - Haiku 5.5 by default (`LEO_MODEL_EXTRACT`; move up only on eval evidence, see `docs/STACK.md`).
  - Prompt plus structured output (`output_config.format` with a JSON schema built from Pydantic models, not forced tool use) that returns terms and invoice facts in the shared value shapes, each with a list of exact quotes.
  - One prompt handles contracts, price notices and invoices.
  - For forwarded email, take the vendor from the forwarded header block, not the internal sender.
  - Store the raw model output.
  - **[must] Response cache** keyed by document hash, stage and prompt version. Reruns then reproduce the same quote spans and keys (D5), and the eval reruns without API calls.
  - Target:
    - every F1–F5 term extracted, including the dispute windows
    - the Lumenpost add-on kept separate from Growth

  Done, in `leo/pipeline/extract_llm.py` (extractor, prompt, mapping), `leo/pipeline/extract_schema.py` (output models) and `leo/llm.py` (structured call and cache; B7 reuses it):
  - **Three requests per document.** The API caps a structured-output schema (16 nullable fields, 24 optional, and a total grammar size). The output is split into three tested parts (`EXTRACTION_PARTS`), sent with the same system prompt and merged. If any part has no complete answer, the extraction is empty with no vendor, so validation fails closed.
  - **The model extracts what is written, and code computes the rest:** an initial term end from a start date and length, the current term end rolled forward to today, notice deadlines (`leo.rules.notice_deadline`) and an escalator's first anniversary. A few fields are set to the key's conventions: no plan label except on price and seat terms, no effective date on notice or dispute windows, one-month billing periods as `YYYY-MM`, `$` as `USD`.
  - **Cache:** `data/llm_cache/extract/`, committed, one file per answer. The key covers the document hash, stage, model, prompt version, effort and a hash of the whole request (system prompt, user prompt and schema). `LEO_LLM_OFFLINE=1` turns a miss into an error instead of an API call.
  - **Eval** (`LEO_LLM_OFFLINE=1 uv run python -m leo.eval --extractor llm`, prompt `extract-v3`, Haiku 5.5 at `medium`):
    - F1–F5 all covered
    - terms: precision and recall 100% (31/31)
    - invoice facts: recall 100%, precision 7/8 (it also records the Harbor supply receipt)
    - contacts: 3/3
    - every quote found
    - the one wrong field is Ferrolink's early-termination fee, where the key drops a "the" the document has
    - a full run costs about $0.02
- [x] **B4 [must] Validator (stage 4).**
  - Each quote must appear in the source, and so must the vendor name.
  - Dates must parse, and amounts must be plausible.
  - Report quotes that aren't in the source as `quote_not_found`. A2 computes the offsets on write (per 0.4).
  - Jev compares each new extraction with the vendor's prior terms.
  - Failures get status `needs_review` instead of being dropped.

  Done, in `leo/pipeline/validate.py` (`RuleValidator`, the comparers) and `leo/jev.py` (the Jev client; B2 can reuse it):
  - **Checks:**
    - `vendor_missing` and `vendor_not_in_source`. Either one makes `vendor_ok` False and sets every term and invoice fact to `needs_review` (fail closed).
    - `quote_not_found`, an exact match with `leo.rules.locate_quote`. A contact's issue has `item=""`, because contacts have no id or status.
    - `date_implausible`: a date outside 2000–2100, or an invoice date after today. `date_unparsed`: a bad `billing_period`.
    - `amount_implausible`: money in (0, 10M], percentages in (0, 100], days 1–366, seats 1–100,000, renewal months 1–120, and a three-letter currency.
    - `conflicts_prior`.
  - **Prior terms.** A prior term is compared only when all of these hold:
    - it comes from another document
    - it has the same type, plan label, agreement ref and effective date
    - for windows, it has the same `applies_to`

    A different effective date is a change, and C1 handles it as supersession.
    - `RulePriorComparer` always runs. It ignores free-text and derived fields.
    - `JevPriorComparer` makes one Jev call per comparable pair. Jev chooses `restates`, `conflicts` or `unrelated`, and a conflict is reported when `conflicts` is its top choice at 0.6 or above. There is no `changes` choice, because only pairs with the same effective date reach Jev; with one, Jev split a same-date $299 vs $339 pair about evenly.
    - If a Jev call fails, that term gets only the rule result, and the engine is marked `rules+jev(error)`. A missing `JEV_API_KEY` stops the run at startup.
    - There is no Jev cache, because the demo is a recorded video.
  - **Eval:** `--validator none|stub|rules|jev`.
    - Every extraction is made first. Each document is then validated against the terms from every other document of the same vendor.
    - A finding counts as covered only if none of its items is `needs_review`.
  - **Results:**
    - The answer key and the B3 cache (`--extractor llm --validator rules`) both give 0 issues and 17/17 `vendor_ok`, with F1–F5 covered.
    - In this dataset, only the forward (02) and its original (01) have comparable prior terms, so they are the only pairs Jev sees.
  - Run the end-to-end test (2.2) with the rules validator, which needs no network and gives the same result every run.
- [x] **B5 [should] Script extractors (stage 2).**
  - A registry of matcher and parser pairs.
  - Hand-write one or two, for example the Kestrel and Quillstack invoice templates.
  - Show in the eval that those items skip the LLM.

  Done, in `leo/pipeline/extract_script.py`:
  - **A script** is a name, a version, a matcher and a parser. `ScriptExtractor` runs the first script whose matcher accepts the raw stored text and records `script:<name>@<version>` as the engine. No match returns None ("not mine").
  - **A changed template falls through.** If a matched script can't parse the document (a missing line, or a line item that doesn't add up to the total), it returns None and keeps the error in `records`, so the LLM takes the document rather than a wrong extraction being stored.
  - **Two scripts** in `SCRIPTS`:
    - `kestrel-invoice@1` (05, 06, 07). It matches the sender `billing@kestrelcloud.example` and an `Invoice: INV-KCH-` line.
    - `quillstack-receipt@1` (08). It matches the sender `billing@quillstack.example`, `Receipt for` and `Order Form: QS-OF-`. The invoice date comes from the `Date:` header, as the key says.

    Each returns one invoice fact with the key's quotes, sliced from the text. A test checks that neither matches any other document.
  - **`ChainExtractor`** returns the first answer that isn't None, in the same order as the worker's extract loop. Wiring the scripts into the worker is 2.1.
  - The registry lives in code. The `extractor_script` table and script generation are Phase 4.
  - **Eval:** `--extractor script` (scripts only, no network) and `--extractor script+llm`. The report shows each document's engine, a count per engine and "reached the LLM: N/M".
  - **Results** (`LEO_LLM_OFFLINE=1 uv run python -m leo.eval --extractor script+llm --validator rules`):
    - 4 of 17 extracted documents go to scripts, and 13/17 reach the LLM.
    - The script facts are all right, with every quote matching the key's.
    - Terms 31/31, invoice-fact recall 100% (precision 7/8, the Harbor receipt as before), contacts 3/3.
    - 17/17 `vendor_ok`, 0 items `needs_review`, and F1–F5 covered.
- [ ] **B6 [should] Cost and funnel numbers.**
  - Count how many items stop at each stage.
  - Track tokens and dollars per run.
  - Expose these through the API for the demo's "16 emails in, N reached the LLM" screen.
- [x] **B7 [must] Action drafting.**
  - Haiku 5.5 by default (`LEO_MODEL_DRAFT`; move up only on eval evidence).
  - Draft a `cancellation_notice`, `renegotiation_request`, `negotiation_email` or `billing_dispute` from a finding and its cited terms.
  - Put in the cited facts (agreement number, amounts, dates, invoice numbers) from a template, and let the LLM write only the surrounding text.
  - Check each draft against `expected_action.must_include` and retry on failure.
  - The recipient comes from a term's `deliver_to`, or else from the vendor's `contacts`.
  - Never redraft an action that already exists (`v1:action:…` key).
  - Drafts are saved with state `draft`.

  Done, in `leo/actions/draft.py`:
  - **Code decides what must be right.**
    - `action_plan` picks the type, alternative and pre-step from the finding's kind.
    - `build_facts` formats the cited facts from the finding and the ledger as named placeholders: the agreement number, amounts, dates as "January 1, 2027", invoice numbers with their dates, and contract clauses quoted verbatim.
    - `recipient` takes a notice or dispute window's `deliver_to`, else a vendor contact ("notices" for a cancellation, "billing" otherwise), else `""`. The approval UI must block an empty `to`.
    - The subject is a template.
  - **The LLM writes only the prose.** `LLMDrafter` (prompt `draft-v5`, Haiku 5.5 at `low`) asks for a body that writes every fact as a `{placeholder}`. Code fills them in.
  - **Check and retry.** The drafter can't see the answer key, so `check_body` stands in for `must_include`. It rejects a body that:
    - leaves out a required placeholder or the word "non-renewal"
    - invents a placeholder
    - types any number itself (numbered-list markers aside)
    - has no paragraph breaks
    - puts a non-clause placeholder in quotation marks
    - repeats a placeholder's opening words before it ("Agreement No. {agreement}")

    The drafter retries up to 3 times, with the problems and the attempt number in the prompt, so a retry misses the cache. If every attempt fails, or a call stops early, it falls back to a fixed template. `TemplateDrafter` is that template alone, with no LLM.
  - **Billing disputes carry no deadline fact.** Their deadline is the last day *we* can raise the dispute. The model misread it as a deadline for the vendor, and the vendor doesn't need it. The approval UI shows it.
  - **No redraft.** `draft_new` skips the `(finding, type)` pairs it's given, before any call. Since 2.1 the worker passes only actions the user decided (approved, sent, rejected) or edited. An unedited draft is redrafted on each reconcile, so it follows its finding's figures, and when nothing changed the draft cache answers.
  - **Cache:** `data/llm_cache/draft/`, committed (7 answers, including 2 retries).
  - **Eval:** `--drafter none|fixture|stub|template|llm` drafts the key's five findings on `fixture.ledger()`. C1 is now built, but it is wired into the eval and the worker in 2.1. It checks type, alternative, `to`, whether there is a pre-step, and every `must_include` string, and it prints each draft.
  - **Results** (`LEO_LLM_OFFLINE=1 uv run python -m leo.eval --drafter llm`): 5/5 drafts pass with no template fallback. The template drafter also scores 5/5.

### Track C: Findings and UI (reconcile and the human gate) (owner TBD after B)

- [x] **C1 [must] Reconciliation rules.** Built and tested on the fixture's correct terms, so it doesn't wait on B. One rule per finding kind:
  - `auto_renewal_deadline` (F1): a notice deadline inside the 60-day lookahead.
  - `price_increase_notice` (F2): a new price term later than the current charge, scoped by `plan_label`. A restated price (the add-on) is not a change.
  - `price_cap_exceeded` (F3): a renewal uplift above `price_cap`.
  - `seats_over_commitment` (F4): billed seats above `seat_count.included`.
  - `escalator_applied_early` (F5): an escalated charge before `first_application`.

  Each finding:
  - records its anchor, linked items, dollar impact, deadline from `deadline_basis` (including dispute windows from `invoice_date`) and `recommended_send_by`
  - uses the `v1:finding:…` key

  C1 also owns supersession: the older price is marked `superseded` once a successor is recorded, and `effective_date` decides which price applies on a given day.

  Tests:
  - F1–F5 match the key on kind, anchor, deadline, send-by date and impact
  - N1–N8 produce nothing

  Deadlines computed during reconciliation are [must]. Rechecking them every day as a queued job in A's queue is [should].

  Done, in `leo/reconcile/reconciler.py` (`RuleReconciler`):
  - **Inputs.** It is pure: it never mutates the ledger and takes `today` from the caller. It ignores `needs_review` terms and invoice facts. It keeps `superseded` terms as price history, but computes supersession from the data and never reads `status`.
  - **Price history.** Prices are grouped per vendor and `plan_label` and ordered by `effective_date`. `price_on(group, day)` picks the price that applies on a day. A price that differs from the current one supersedes it. An equal price is a restatement, which changes nothing: this is the add-on, N3.
  - **Rules.** One per kind, with deadlines from `leo.rules`:
    - F1: an auto-renewing term whose notice deadline is within the lookahead (`lookahead_days`, 60).
    - F2: a price increase not yet in effect. The deadline is the day before it takes effect.
    - F3–F5: invoice facts checked against the cap, the seat count and the escalator date. Each takes the earliest dispute deadline still open among its offending invoices, or None when there is no dispute window (F4).
  - **Linked items.**
    - transactions: the latest charge (F1, F2), or the last charge before the first offending invoice plus the offending invoices' charges (F3–F5)
    - `documents`: the anchor term's document plus the documents of the invoice facts
    - `linked_duplicates`: probable duplicates of those documents. Exact duplicates are one row (D1).
    - `plan_label`: set only when the vendor has more than one plan
  - **Ids.** `v1:finding:{anchor term}:{anchor txn or -}:{kind}`, from the ledger's ids. They become natural keys once A2's ids flow through. Titles and `deadline_notes` are templates, with a weekend note when the send-by date moves.
  - **Tests** (`tests/test_reconcile.py`):
    - F1–F5 match the key on every structured field. Only id, title and notes are left out.
    - Supersessions: one, Growth v1 → v2.
    - The result is the same when every status is `current`.
    - N1–N8 stay quiet.
    - Edge cases: a `needs_review` notice, the lookahead bounds, and a later `today` (F2 drops out, and F3 moves to the October invoice).
  - **Not built yet:** the daily recheck (needs A4), and running on B3's extractions (Phase 2.1).
- [x] **C2 [must] UI on fixture data.** Start in Phase 1 against the fixture loader, then switch to the API.
  - **Dashboard:** open findings, days left, dollars at stake.
  - **Finding detail:** click any number to see the source document with the quote highlighted.
  - **Approval queue:**
    - shows the draft, its recipient, `pre_step` (for example "confirm with Sam first") and any `alternative`
    - edit, approve or reject
    - "Send" is simulated: the draft is written to an outbox and marked `sent`, and nothing is sent externally.
  - **[should]:** field correction and rejection, saved as `feedback_event` rows.

  Done, as a React UI (`web/src/`) on a fixture-backed API (`leo/api/`):
  - **Store.** `FixtureStore` (`leo/api/store.py`) serves the key's findings (`FixtureReconciler`) with drafts from the committed LLM cache (`LLMDrafter`, forced offline). A miss or a retry fallback uses the template, and the action shows `engine: template`. It isn't `RuleReconciler` yet: the draft cache key includes the finding id, and the cache was recorded on F1–F5, so C1's `v1:finding:…` ids would miss it. Phase 2.1 wires C1 in and recaches. Action state and the outbox are in memory, so a restart resets them.
  - **Routes.** `GET /api/dashboard`, `/api/findings` (sorted by days left, with no deadline last, then dollars at stake), `/api/findings/{id}`, `/api/documents?path=` (dataset paths only), `/api/actions` and `/api/outbox`. `PATCH /api/actions/{id}` edits a draft. `POST …/approve|reject|send` moves it through draft → approved → sent, or draft → rejected, and `…/restore` undoes a reject. Any other move is a 409, and a blank edit field is a 422.
  - **Click any number.** `leo/api/sources.py` maps each deadline, impact and computation field to the terms and invoice facts it comes from (for example, F3's cap % comes from the `price_cap` term). A field not in the table maps to all of the finding's items. Quotes are located with `locate_quote` on the raw text, and the viewer highlights `[start, end)`.
  - **UI.** The pages are the dashboard, the finding detail with a source viewer, and the approval queue with the outbox. Hash routes and plain CSS, with no new dependencies.
  - **Tests** (`tests/test_api.py`): the sort order, every quote located, every number sourced from linked items, document paths limited to the dataset, all 5 drafts from the cache, and the action transitions.
  - **Not built yet:** the [should] corrections (they need A's `feedback_event`).
  - **Since C3:** `FixtureStore` is gone. The API reads Postgres, and the drafts are the seed's (`engine: fixture`), not the LLM cache's.
- [x] **C3 [must] API.**
  - Findings, sorted by deadline and impact.
  - Finding detail with its terms and citations.
  - Source documents with quote locations.
  - The action queue: approve, edit, reject.
  - Upload (calls A3's `ingest`).
  - Stage and cost stats from B6.
  - **[should]:** corrections.

  Done, in `leo/db/read.py`, `leo/api/store.py` (`DbStore`) and `leo/api/routes.py`:
  - **Reads.** `load_ledger` and `load_findings` rebuild the shared types from one tenant's rows. Ids are `str(uuid)` and document references are paths. B and C switch over to these.
    - Link order isn't stored, so linked items are sorted by their own fields.
    - A finding's `documents` follows C1's rule: the anchor term's document, then the invoice facts' documents.
    - Citations come back with their stored offsets.
  - **Store.** `DbStore` keeps C2's routes and response shapes. It serves whatever findings and actions the database holds: the seed's key rows now, the pipeline's after 2.1. The tenant is `LEO_TENANT` (default `brightwater-v1`). A missing tenant gives a 503 that says to run `make seed`.
    - An id that isn't a UUID gives a 404.
    - The drafts are the seed's `FixtureDrafter` text, so `engine` is `fixture`. The engine is read from `raw_model_output["engine"]` once the drafter stores one.
  - **Actions.** Each transition is one conditional `UPDATE`, so decisions persist across restarts and reseeds.
    - Approve records `approver` (`LEO_APPROVER`, default `reviewer`, since there's no login yet) and `decided_at`. Restore clears them.
    - Migration `3f5925a397fe` adds `action.edited_at` and `sent_at`. `Repo.upsert_action` now updates draft text only while the action is an unedited draft.
    - The outbox is the sent actions.
  - **Upload.** `POST /api/documents` (multipart) calls `ingest` and commits. The path is `uploads/{sha8}-{name}`, and bytes already stored keep their row and path. A dashboard file picker uses it. Since A4 it queues a `classify` job.
  - **Stats.** `GET /api/stats` counts documents by class, terms and invoice facts by status, current findings, actions by state, and jobs by stage, `processed_by` and status (empty until A4). Cost comes with B6.
  - **Seed.** `make seed` runs `python -m leo.db.seed` and commits. A reseed doesn't undo decisions, so a clean demo needs `make reset` (A7).
  - **Tests** (`tests/test_api.py`, which now needs Postgres): each test seeds under a random tenant slug, so the dev database never affects a test. `tests/test_repo.py` does the same now, because `make seed` commits the default tenant to that database. They cover:
    - C2's checks, run on the database
    - `load_findings` matching the key's F1–F5 on every field but the ids
    - decisions and edits surviving a reseed
    - upload deduplication
    - the stats counts
  - **Not built yet:** the [should] corrections. `GET /api/documents?path=inbox/07_…_resynced.eml` is now a 404, because D1's copy is the original's row.
- [x] **C4 [should] Export** of the term record and findings to CSV.

  Done, in `leo/api/export.py` (pure formatting) and `DbStore.export_terms`/`export_findings`. It reads through C3's `leo/db/read.py` and adds no new queries.
  - **Endpoints.** `GET /api/export/terms.csv` and `GET /api/export/findings.csv` download as `leo-{terms|findings}-{today}.csv`. The dashboard links to both.
  - **Term record.** One row per stored term, in every status, so supersessions and the price history show. Each term-value key gets its own column: the union of the `*Value` models' fields, taken from `model_fields`, blank where a type lacks the key. Rows sort by vendor, type and effective date (undated last).
  - **Findings.** The dashboard's rows and order. `impact` and `computation` are `key=value; …` text, because their shapes differ by kind.
  - **Formatting.** Decimals are exact (`str`, as stored), dates are ISO, and the file has a UTF-8 BOM for Excel. A text cell starting with `= + - @`, a tab or a CR gets a leading `'`, because quotes come from vendor emails. Numbers are never prefixed.
  - **Tests** (`tests/test_api.py`): every key term exported, Kestrel's cap exact, superseded rows linked, findings in dashboard order with the key's `at_stake`, and the cell formatting.
- [x] **C5 [should] Vendor view.** A vendor's terms, price history (a query per the design doc, leaving out terms from linked duplicates) and matched transactions.

  Done, in `load_price_history` (`leo/db/read.py`), `DbStore.vendors`/`vendor` and `web/src/Vendors.tsx`/`VendorDetail.tsx`.
  - **Price history.** The design doc's query: the vendor's `price` terms, one series per `agreement_ref` and `plan_label`, ordered by effective date (undated last). Terms on a document linked as a `probable_duplicate` of another are left out, so a forward counts once (D2). `needs_review` prices are left out too, as in the reconciler, and so are `needs_review` invoice facts when matching charges to invoices.
    - Each point has `in_effect` (effective on or before the clock's today) and `change`, the amount minus the previous point's. `change` is empty when the currency or `per` differs.
    - The UI labels the latest point in effect as current, earlier ones as superseded, and later ones as not yet in effect.
  - **Endpoints.** `GET /api/vendors` lists each vendor's term, transaction and open-finding counts and its dollars at stake. `GET /api/vendors/{id}` returns the vendor's findings in dashboard order, its price history, its terms in every status (duplicates left out), its transactions by date and its linked duplicates. Each transaction carries its plan, the invoice fact that matches it and the findings that cite it. An unknown id, a non-UUID or another tenant's vendor gives a 404.
  - **UI.** A Vendors tab with the vendor list, sorted by dollars at stake. A vendor page laid out like the finding detail: totals, findings, a price timeline, the other terms, and card charges with their invoices. Quotes open in the source viewer. Vendor names on the dashboard and the finding detail link to it.
  - **Tests** (`tests/test_api.py`): the list's counts match the key and sum to the dashboard, Lumenpost's history is D4's, a term added on the forwarded email (02) stays out, unvalidated (`needs_review`) prices and invoice facts stay out of the history and the matches, Kestrel's invoices sit on the key's transactions, every key term appears once under its vendor, and the 404s.
  - **Not done:** `load_ledger` still returns terms and invoice facts from linked duplicates, though the `Ledger` docstring says it leaves them out. The seed has none, so nothing breaks yet. The vendor view filters them itself.

---

## Phase 2: Integration (all three)

**Starts when** A2–A6, B3, B4, B7 and C1 have landed. Pair up across tracks here instead of working alone.

- [x] **2.1 [must] Wire the pipeline.** The worker runs these stages in order, writing to Postgres at each step:
  1. ingest
  2. classify
  3. script or LLM extract
  4. look up the document's vendor (no create; validate needs its prior terms)
  5. validate, then create the vendor only if the vendor name checks out (`vendor_ok`)
  6. dedup link
  7. transaction match
  8. reconcile
  9. draft

  The API (C3) reads whatever findings and actions are in Postgres, so it needs no change: write findings, links and actions through `Repo`, and set `action.raw_model_output.engine`. One API change is due first: `DbStore` shows only the first action per finding and drops findings with no action (`leo/api/store.py`, `_view`). The seed always writes exactly one, but the worker may write a finding before its draft, or several actions per finding (the key includes `action_type`). An approve or send on a hidden action would commit and then return 404 (PR #14 review). Then the B7 draft cache can be re-recorded on the new finding ids, if the demo should show LLM drafts.

  Before wiring B7's LLM drafter, make the reconcile job per tenant or debounce it. A4 keys it per document, and each one reconciles and drafts the whole ledger, so N documents mean N rounds of LLM calls (A4 review).

  Worker robustness (PR #17 review):
  - `recover_stale` resets any job claimed more than `STALE_AFTER` (10 minutes) ago, even if its worker is still running it. Before running more than one worker, or stages that can take longer than that, tie `STALE_AFTER` to a stage timeout or refresh `claimed_at` as a heartbeat. Otherwise a slow LLM stage runs twice.
  - Any database error (in `claim`, `_failed` or `_recover`) stops `serve()`. The job stays `running` until `recover_stale` picks it up after a restart. Before running the worker unattended, add a supervisor restart policy or retry with backoff around the loop.

  Prior terms come from the store with their validated status, so a `needs_review` prior isn't compared. The eval (`leo/eval/runner.py`) builds priors from raw extractions, all `current`; make it match the worker here (PR #3 review).

  Done, in `leo/worker/` (`run.py`, `writes.py`, `pipeline.py`, `queue.py`), `leo/db/write.py`, `leo/ingest/folder.py` and migration `b21a9e0c4d17`:
  - **Run.** `make ingest` loads the synthetic documents into their own tenant (`brightwater-pipeline`, `LEO_PIPELINE_TENANT`) and queues them. `make worker ARGS="--drain --tenant brightwater-pipeline"` runs them, and `make api-pipeline` serves the result. `make seed` still serves the key's rows, in its own tenant.
  - **Engines** (`Pipeline.default`, `python -m leo.worker --help`): Jev classifier (`--classifier fixture` for the key's classes), B5's scripts then B3's LLM extractor, the rules validator (`--validator jev` adds Jev's prior comparison), `DbVendorResolver`, C1's `RuleReconciler`, and B7's `LLMDrafter` (`--drafter template`). `--stubs` keeps A4's stubs.
  - **Writes, per stage, in the job's transaction.**
    - classify: the document's class.
    - validate: the vendor (created only on `vendor_ok`) and the terms, invoice facts and contacts, through `leo/db/write.py`, which the seed now uses too. Prior terms come from the store, `needs_review` ones left out. A document that fails closed stores its items as `needs_review` with no vendor: the migration makes `vendor_id` nullable on terms and invoice facts, and lets a `needs_review` fact keep a period that doesn't parse.
    - reconcile: `sync_transactions` (after the documents, so vendor names come from documents first), `link_duplicates`, `match_plans` and `match_invoice_facts`, then the reconciler. Supersessions are recomputed whole. Findings are keyed `keys.finding(anchor term key, anchor txn key, kind)` from the stored natural keys, and their links are replaced as a set. A finding no longer produced becomes `superseded`, and comes back `current` if it reappears. `draft_new` drafts each (finding, type) whose action the user hasn't decided or edited, with the finding's natural key as its id, so the draft cache survives `make reset`. An unedited draft therefore carries the finding's current figures. Approved, sent, rejected and edited actions are never rewritten. An LLM draft that fell back to the template records `engine: template`.
  - **One reconcile per tenant.** The reconcile job is keyed `v1:job:tenant:{tenant}:reconcile`, with no document. Each validated document requests it (`queue.request_reconcile`): a pending request is left alone, and a finished one goes back to `pending`. `claim` takes it only once the tenant has no other job pending or running, so N documents give one round of drafting. Jobs are created with `clock_timestamp()`, so jobs queued in one transaction run in the order they were queued, and the first document of each vendor names it the same way every run. `--drain` waits out a retry's backoff instead of stopping, because stopping would leave the reconcile unrun. `run_after` and `claim` use the wall clock for that reason. It doesn't wait for a reconcile that `claim` would refuse because another job is running (`seconds_until_due` shares claim's check), and it recovers stale jobs before each wait.
  - **API.** `DbStore` serves every current finding, with or without an action. A finding's row shows its oldest action, and the queue, outbox and action routes see every action of a current finding, so approving a second action no longer commits and then 404s (PR #14 review). The actions of a superseded finding are kept: the outbox still lists a sent one, and a transition on one answers normally. The queue only offers current findings' actions. The UI shows "no draft" for a finding without one.
  - **Fixes the end-to-end run needed.**
    - F3 never fired: the LLM wrote the Kestrel price cap's `agreement_ref` as "MSA No. KCH-MSA-2209" and the renewal's as "KCH-MSA-2209", and `price_caps` joins on exact equality. `LLMExtractor` now keeps only the identifier (`agreement_ref()`). The eval scores `agreement_ref` from now on, but leaves it out of finding coverage, and a test checks that a vendor's cached terms agree on it.
    - From the A6 and B5 follow-ups: the forward check reads the decoded email body, numbers in term values are normalized before fingerprinting, scripts match CRLF mail (Gmail), and `ScriptExtractor.records` keeps the latest 100.
    - The eval's prior terms carry their own validated status, as in the worker.
    - A failed-closed invoice fact (no vendor) never matches a charge with no vendor (code review).
  - **Result** (`make reset && make ingest`, then the worker with Jev and the LLM drafter, 2026-11-10): 54 jobs, none stuck. F1–F5 match the key on kind, anchor, deadline, send-by date, impact amount, linked items and duplicates. The one difference is F1's free-text impact `basis`, which is C1's template. No near-miss produced anything. D2's forward is linked, and there is one supersession (Growth v1 → v2). The dashboard shows $17,184 at stake, the same as the seed. 13 of 16 extracted documents reach the LLM. A second reconcile round changes no row or id. Re-recording the draft cache added 7 answers (`data/llm_cache/draft/`), and an offline run after `make reset` hits all of them.
  - **Draft quality.** 4 of 5 drafts are the LLM's. F4's billing dispute (Quillstack) fails `check_body` three times (it repeats the invoice list before the `{invoices}` placeholder), so it uses the template and shows `engine: template`. The eval's `--drafter llm` still passes 5/5 on the key's findings, whose facts differ slightly. 2.3 should look at it.
  - **Eval.** With `agreement_ref` scored, `--extractor script+llm` shows 20/31 terms right on it: the LLM leaves it empty on Lumenpost's and Ferrolink's terms. No finding depends on it.
  - **Tests.** `tests/test_pipeline.py` covers the stage writes, fail-closed storage, file order, one reconcile after the rest, re-requests and the debounce. On the synthetic set (offline, `--classifier fixture`, template drafts) it covers F1–F5 and nothing else, D2, one supersession, a draft per finding, the vendors, an idempotent second round, no redraft of an edited action, and findings that vanish and come back. `tests/test_api.py` covers a finding with no action and approving a second action.
  - **Not done here.**
    - Several workers: a validate that finishes while reconcile is running isn't seen until the next request. The worker-robustness items from PR #17 (`STALE_AFTER` heartbeat, `serve()` retry on database errors) still apply.
    - A document re-extracted with different quote spans leaves its old terms. The cached extraction always gives the same spans.
    - Vendors are named by their first document, as written: `CORVANA, INC.` and `Lumenpost Billing` appear in titles and drafts.
    - Gmail mail is CRLF and keyed differently, so the LLM extract cache misses for it and the worker calls the API.
    - Contacts are keyed per vendor, email and purpose, so 01's and 02's shared contact is one row cited by the later document.
    - Each validate job loads the tenant's whole ledger for its prior terms. That's fine at demo scale.
    - `agreement_ref()` keeps the last token with a digit, so a ref written with a trailing date or amendment number ("MSA KCH-2209 dated 2025-01-01") keeps the wrong token. The cache has none.
    - The migration's downgrade fails if rows were stored with no vendor or a period that doesn't parse. Delete them first.
    - A script quote that spans lines wouldn't be found in CRLF text. Both scripts quote single lines.

- [ ] **2.2 [must] End-to-end test** (`make e2e`):
  - reset and seed, then run the pipeline on the synthetic set
  - assert F1–F5 are found, N1–N8 are not, and D1–D5 pass
  - from here on, run it before every merge to main
- [ ] **2.3 [must] Fix the gaps** that only show up end to end:
  - vendor names from extraction not matching transactions
  - plan labels not lining up
  - terms that failed validation hiding a finding
- [ ] **2.4 [must] Accuracy bar.** Agree on the target and record the numbers from `make eval` and `make e2e`. Proposed target:
  - 100% of the planted findings
  - 0 false findings
  - at least 90% field-level accuracy on terms

**Exit:** one command takes an empty database to five correct findings, each with a drafted action, in a few minutes.

## Phase 3: Demo (all three)

- [ ] **3.1 [must] Demo script.** About 3 minutes, in this order:
  1. the problem
  2. live ingest
  3. the stage funnel ("16 emails in, N reached the LLM")
  4. the five findings and the dollars at stake
  5. click-through to the source
  6. approving one draft
  7. the near-misses LEO correctly ignored
- [ ] **3.2 [must] Demo safety.**
  - Pin `LEO_TODAY`.
  - Run from the committed LLM caches (B3, B7) so reruns give the same terms and drafts. Jev calls (B2, B4) are live and have no cache. That's fine because the demo is a recorded video, so it doesn't need to run offline.
  - One-command reset.
  - A recorded backup video.
- [ ] **3.3 [should] Live inbox.** If A8 is ready, show a new price-change email arriving in Gmail and becoming a finding.
- [ ] **3.4 [should] Pitch.**
  - The Ramp tie-in: reminders track end dates, while LEO checks bills against terms and email.
  - The cost story.
  - The path to pooling vendor spend across multiple entities.
- [ ] **3.5 [must] Rehearse twice** on the demo machine.

## Phase 4: Stretch (only after the demo is green)

- [ ] **[stretch] Script generation.** When the LLM extracts from the same sender or template several times, a coding model proposes a script. It is promoted only if it matches past verified results.
- [ ] **[stretch] Feedback loop.** Corrections become few-shot examples in the extraction prompt, and they lower a script's accuracy until it is rewritten.
- [ ] **[stretch] PDF contracts.** Text extraction with page and line locations for citations.
- [ ] **[stretch] Ramp sandbox** behind `TransactionSource`, if we get access.
- [ ] **[stretch] Hardening.** Row-level security per tenant, access logging, and a retention policy.
- [ ] **[stretch] Multi-entity view.** Pool vendor spend across several tenants.

---

## Open questions: who decides and by when

From the design doc's Open Questions. Track B questions belong to Lewis and Omar. Track A questions go to the Track A owner once one is chosen.

| Question | Owner | Decide by | Blocks |
| --- | --- | --- | --- |
| Jev noise threshold | Lewis and Omar | Resolved 2026-11-05: 0.5, see B2 | B2 |
| Ramp data: sandbox or synthetic | Lewis | Phase 0 | A5 (synthetic is the default) |
| Where dedup runs: before or after triage | Lewis | Resolved 2026-11-10: after extraction, on the stored terms and invoice facts, see A6 | A6 |
| Ambiguous vendor names (processor descriptor versus legal name) | A owner (TBD) | Resolved 2026-11-10: processor-prefix list, then trigram candidates on suffix-stripped names confirmed by a word check, see A5 | A5 |
| Accuracy bar | All | Phase 2 start | 2.4 |
| Inbox scopes and retention we promise | A owner (TBD) | Before A8 | A8, pitch |
| Script generation threshold and testing | Lewis and Omar | Phase 4 | Stretch |
| Corrections: prompts and scripts only, or also kept for fine-tuning | Lewis and Omar | Phase 4 | Stretch |
| ~~Price change by email versus the signed contract: which wins~~ | Closed | — | The key settles it in F2: a properly noticed email change is a legitimate new price term that supersedes the old one, and the finding is about acting before it takes effect. |
| ~~Demo data: which five issues~~ | Closed | — | Answered by `data/synthetic/` |
