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

- **One branch per task**, named `<task-id>-<slug>` (for example `0.6-fixture-loader` or `A1-schema`).
- **Each PR is reviewed by one teammate** before it merges.
- **Any change to `leo/types.py` or `leo/interfaces.py` needs all three to agree.**

**Order.** Track B comes first, built by Lewis and Omar together. Once it is done, we decide who takes Track A and who takes Track C.

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
  - Claude Haiku 5.5 investigates ambiguous emails, extracts terms and drafts emails. A job moves to a heavier model only on eval evidence.
  - Frontend: React + Vite + TypeScript.
- [x] **0.2 [must] Repo skeleton.**
  - Package layout: `leo/` with `db/`, `ingest/`, `pipeline/`, `reconcile/`, `actions/` and `api/`, plus `web/` for the frontend.
  - `docker-compose.yml`, and `.env.example` with `ANTHROPIC_API_KEY`, `JEV_API_KEY`, `LEO_MODEL_INVESTIGATE`, `LEO_MODEL_EXTRACT`, `LEO_MODEL_DRAFT`, the database URL and `LEO_TODAY`.
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

- [ ] **A1 [must] Schema and migrations.**
  - The first Alembic migration creates:
    - every table in the design doc: `tenant`, `vendor`, `vendor_alias`, `source_document`, `contract_term`, `citation`, `transaction`, `finding`, `action`, `extractor_script`, `feedback_event` and `job`
    - four additions: `invoice_fact`, `vendor_contact`, `document_link` for duplicate links, and `finding_term` / `finding_transaction` / `finding_invoice_fact` for linking a finding to many items
  - Every table has `tenant_id`, a UUIDv7 primary key, and `UNIQUE (tenant_id, natural_key)` where it has a natural key.
  - Raw model output goes in JSONB.
  - Enable `pg_trgm`.
- [ ] **A2 [must] Keys and upserts.**
  - Natural-key builders: `v1:gmail:…`, `v1:eml-hash:…`, `v1:file:…`, `v1:term:…`, `v1:invoice-fact:{doc key}:{first quote start}:{first quote end}`, `v1:finding:{anchor term key}:{anchor txn key or -}:{kind}`, `v1:txn:{source}:{external_id}`, `v1:job:{doc key}:{stage}` and `v1:action:{finding key}:{action type}`.
  - A repository layer where every write is an upsert.
  - Tests:
    - Running the seed twice gives the same rows and IDs.
    - D1 (the byte-identical re-sync) gives one document row.
    - D5 (a full rerun) adds no rows to any table.
  - ⇄ B and C switch from fixtures to the repository once this lands.
- [ ] **A3 [must] Ingestion function.**
  - An `.eml` parser covering headers, text body, and attachments stored once by hash.
  - A contracts-folder loader.
  - `ingest(file)` for the upload endpoint (C3) to call.
  - Raw files go in a local folder, and the database stores references.
  - `Message-ID`, `threadId` and similar headers are attributes, never part of a key.
- [ ] **A4 [must] Job queue and worker.**
  - Jobs are rows claimed with `FOR UPDATE SKIP LOCKED`.
  - The worker runs each item through the stage interfaces in `leo/interfaces.py`, using `leo/stubs.py` until B and C plug in.
  - It records which stage handled each item (`processed_by`) and retries or parks failures.
- [ ] **A5 [must] Transactions, vendors and matching.**
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
- [ ] **A6 [must] Duplicate linking.**
  - Compute `context_fingerprint` and suggest links per the design doc's table, linking on the fingerprint alone plus a forwarded-message check (`Fwd:` subject, forwarded header block), because the dataset has no attachments.
  - Build and test it on the fixture's terms first, so it doesn't wait on B.
  - Terms from a linked duplicate are not counted again.
  - Tests:
    - D2: the forward is linked.
    - D3: monthly invoices stay separate.
    - D4: a two-plan vendor keeps separate price histories.
- [ ] **A7 [should] Seed and reset.** `make seed` loads the synthetic tenant, and `make reset` reruns migrations and seed. Deterministic keys make this safe.
- [ ] **A8 [should] Gmail sync.**
  - Read-only OAuth into a seeded demo Gmail account.
  - Incremental sync by `historyId`.
  - Tokens encrypted at rest.
  - The `.eml` path stays as the fallback, so the demo never depends on this.

### Track B: AI pipeline (stages 1–4 and drafting) (Lewis and Omar, first)

- [ ] **B1 [must] Eval harness first.** `make eval` scores against `expected.json`:
  - classification accuracy, and recall on the documents that matter
  - term precision and recall on vendor, type, value, effective date and plan
  - whether each quote matches, reusing `find_quote` from `scripts/validate_answer_key.py`

  It prints a per-document report. This is how B knows when it's done.
- [ ] **B2 [must] Classifier (stage 1).**
  - **[must]:** a `Classifier` interface with Jev (TypeSafe AI) as the classifier.
  - **[must]:** items below Jev's confidence threshold go to a Haiku 5.5 investigate step (`LEO_MODEL_INVESTIGATE`). If Jev is unavailable, every item takes this path.
  - Build a labeled set from the 22 documents.
  - Choose the "noise" threshold that keeps 100% recall on the planted issues, and report the false-pass rate.
  - Send a sample of the items it labels noise to the LLM as a tripwire.
- [ ] **B3 [must] LLM extraction (stage 3).**
  - Haiku 5.5 by default (`LEO_MODEL_EXTRACT`; move up only on eval evidence, see `docs/STACK.md`).
  - Prompt plus structured output (`messages.parse` with the Pydantic models, not forced tool use) that returns terms and invoice facts in the shared value shapes, each with a list of exact quotes.
  - One prompt handles contracts, price notices and invoices.
  - For forwarded email, take the vendor from the forwarded header block, not the internal sender.
  - Store the raw model output.
  - **[must] Response cache** keyed by document hash, stage and prompt version. Reruns then reproduce the same quote spans and keys (D5), and the demo can run offline.
  - Target:
    - every F1–F5 term extracted, including the dispute windows
    - the Lumenpost add-on kept separate from Growth
- [ ] **B4 [must] Validator (stage 4).**
  - Each quote must appear in the source, and so must the vendor name.
  - Dates must parse, and amounts must be plausible.
  - Report quotes that aren't in the source as `quote_not_found`. A2 computes the offsets on write (per 0.4).
  - Jev compares each new extraction with the vendor's prior terms.
  - Failures get status `needs_review` instead of being dropped.
- [ ] **B5 [should] Script extractors (stage 2).**
  - A registry of matcher and parser pairs.
  - Hand-write one or two, for example the Kestrel and Quillstack invoice templates.
  - Show in the eval that those items skip the LLM.
- [ ] **B6 [should] Cost and funnel numbers.**
  - Count how many items stop at each stage.
  - Track tokens and dollars per run.
  - Expose these through the API for the demo's "16 emails in, N reached the LLM" screen.
- [ ] **B7 [must] Action drafting.**
  - Haiku 5.5 by default (`LEO_MODEL_DRAFT`; move up only on eval evidence).
  - Draft a `cancellation_notice`, `renegotiation_request`, `negotiation_email` or `billing_dispute` from a finding and its cited terms.
  - Put in the cited facts (agreement number, amounts, dates, invoice numbers) from a template, and let the LLM write only the surrounding text.
  - Check each draft against `expected_action.must_include` and retry on failure.
  - The recipient comes from a term's `deliver_to`, or else from the vendor's `contacts`.
  - Never redraft an action that already exists (`v1:action:…` key).
  - Drafts are saved with state `draft`.

### Track C: Findings and UI (reconcile and the human gate) (owner TBD after B)

- [ ] **C1 [must] Reconciliation rules.** Built and tested on the fixture's correct terms, so it doesn't wait on B. One rule per finding kind:
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
- [ ] **C2 [must] UI on fixture data.** Start in Phase 1 against the fixture loader, then switch to the API.
  - **Dashboard:** open findings, days left, dollars at stake.
  - **Finding detail:** click any number to see the source document with the quote highlighted.
  - **Approval queue:**
    - shows the draft, its recipient, `pre_step` (for example "confirm with Sam first") and any `alternative`
    - edit, approve or reject
    - "Send" is simulated: the draft is written to an outbox and marked `sent`, and nothing is sent externally.
  - **[should]:** field correction and rejection, saved as `feedback_event` rows.
- [ ] **C3 [must] API.**
  - Findings, sorted by deadline and impact.
  - Finding detail with its terms and citations.
  - Source documents with quote locations.
  - The action queue: approve, edit, reject.
  - Upload (calls A3's `ingest`).
  - Stage and cost stats from B6.
  - **[should]:** corrections.
- [ ] **C4 [should] Export** of the term record and findings to CSV.
- [ ] **C5 [should] Vendor view.** A vendor's terms, price history (a query per the design doc, leaving out terms from linked duplicates) and matched transactions.

---

## Phase 2: Integration (all three)

**Starts when** A2–A6, B3, B4, B7 and C1 have landed. Pair up across tracks here instead of working alone.

- [ ] **2.1 [must] Wire the pipeline.** The worker runs these stages in order, writing to Postgres at each step:
  1. ingest
  2. classify
  3. script or LLM extract
  4. look up the document's vendor (no create; validate needs its prior terms)
  5. validate, then create the vendor only if the vendor name checks out (`vendor_ok`)
  6. dedup link
  7. transaction match
  8. reconcile
  9. draft
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
  - Run from B3's response cache with no network.
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
| Jev confidence threshold and the cutoff for the Haiku investigate step | Lewis and Omar | During B2 | B2 |
| Ramp data: sandbox or synthetic | Lewis | Phase 0 | A5 (synthetic is the default) |
| Where dedup runs: before or after triage | A owner (TBD) | Before A6 | A6 |
| Ambiguous vendor names (processor descriptor versus legal name) | A owner (TBD) | During A5 | A5 |
| Accuracy bar | All | Phase 2 start | 2.4 |
| Inbox scopes and retention we promise | A owner (TBD) | Before A8 | A8, pitch |
| Script generation threshold and testing | Lewis and Omar | Phase 4 | Stretch |
| Corrections: prompts and scripts only, or also kept for fine-tuning | Lewis and Omar | Phase 4 | Stretch |
| ~~Price change by email versus the signed contract: which wins~~ | Closed | — | The key settles it in F2: a properly noticed email change is a legitimate new price term that supersedes the old one, and the finding is about acting before it takes effect. |
| ~~Demo data: which five issues~~ | Closed | — | Answered by `data/synthetic/` |
