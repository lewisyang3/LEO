# Demo video (ROADMAP 3.1, 3.3)

A recorded video, about 3 minutes, of the product only: no slides. The live moment is a real
Gmail sync. A vendor's price-change email lands in the demo inbox, and LEO turns it into a
finding on the dashboard.

Every figure here comes from the dry run of 2026-11-10 (the API's output for `gmail-demo`), not
from the answer key.

## How the demo tenant is fed

The tenant is `gmail-demo` (`LEO_GMAIL_TENANT`), fed the way a customer's would be:
- **Contracts** are loaded as files (`data/synthetic/contracts/`).
- **Emails** come from the connected Gmail inbox, read-only.
- **Card charges** come from `data/synthetic/transactions.csv`, which the worker syncs at every reconcile.

The live email is **01, the Lumenpost price-change notice**, held back from the first import.
It carries "Account ID: LP-204417", so its new price supersedes the order form's (F2).

Two other emails stay out of the demo inbox:
- **02** is a forward of 01. With it in the inbox, F2 would exist before the live moment.
- **07** is a byte-identical resync of 06. Over Gmail each copy gets its own message id, so the two would be stored twice. Dedup (D1–D5) is covered by `make e2e`, not by this video.

Classification is live Jev, the worker's default.

Gmail adds its own `Received` headers to each import. LEO sends the raw email to the LLM, so the
Gmail copies don't hit the cache committed for the `.eml` files. The first `demo-prep` called
Haiku live, and its cache entries are committed, so the 13 emails replay the same on every
retake. The live email is a fresh import each take, so its 3 extract calls and its draft are
always live.

## One-time setup

1. The demo Gmail account exists and is a test user on the OAuth client's consent screen.
2. `var/google_client_secret.json` and `LEO_SECRET_KEY` (in `.env`) are in place. `LEO_TODAY=2026-11-02` is pinned in `.env`.
3. Import the inbox, minus the held-back emails. **Run this once only**: the import isn't idempotent, and a second run duplicates every email.

   ```
   make gmail-import ARGS="--skip 01_lumenpost_price_change_notice.eml --skip 02_fw_lumenpost_price_change_notice.eml --skip 07_kestrel_invoice_2026_10_resynced.eml"
   ```

   Sign in as the demo account. This consent is for `gmail.insert`, and that token is never stored.

## Before each take

1. In Gmail, move 01 to the trash if an earlier take imported it. A first sync lists only `INBOX`.
2. `make demo-prep`. It runs these steps:
   1. Reset the database. This reseeds `LEO_TENANT`, never the Gmail tenant.
   2. Load the contracts into `gmail-demo`.
   3. `gmail-connect`: browser consent, read-only. The reset dropped the stored token.
   4. Sync: 13 new emails.
   5. Drain the worker.

   The result: 19 documents, 4 findings, $16,704 at stake, 4 drafts.
3. Start these, each in its own terminal:
   - `make api-gmail`
   - `make web`
   - `make worker ARGS="--tenant gmail-demo"`
   - `make gmail-sync ARGS="--watch 10"`
4. Open the dashboard and the demo Gmail inbox in two browser tabs.

## The live trigger (off camera)

```
make gmail-import ARGS="--only 01_lumenpost_price_change_notice.eml --live"
```

`--live` gives the email a fresh Message-ID and Gmail's receipt time. Without it, Gmail matches
the copy from an earlier take (even in Trash) and only moves it back to the Inbox, which sync
doesn't read as a new message, and it would sort at its Oct 14 date. The Date header is kept.

The email appears unread at the top of Gmail. Within about 10s the sync picks it up, and the worker
classifies, extracts, validates, reconciles and drafts. The dashboard refetches every 5s.

## Script

About 3 minutes. Times are a guide.

| # | Time | On screen | Said |
| --- | --- | --- | --- |
| 1 | 0:15 | The dashboard: 4 findings, $16,704 | "Vendor terms live in contracts and emails: price caps, escalators, seat commitments, notice windows. Bills drift from them, and notice windows lapse. Nobody reads every invoice against the contract." |
| 2 | 0:50 | The Gmail tab: Lumenpost's price-change email lands at the top, unread. A terminal: the sync reports 1 new, and the worker runs classify, extract, validate, reconcile. The dashboard: a fifth finding, "Lumenpost Growth rises 13.4% ($299.00 to $339.00) on Dec 1, 2026", and $17,184 at stake. | "A vendor emails a price change. LEO reads the inbox read-only, sees it's a price notice, pulls out the new price, and checks it against the order form and what the card was charged." |
| 3 | 0:20 | The funnel panel: 20 documents in, 5 stopped as noise, 3 read by scripts, 12 reached the LLM | "Most of it never reaches a model. Of 20 documents, 5 were noise and stopped at classification. Known invoice formats are read by scripts. Only 12 reached the LLM, Claude Haiku, for about two cents for the whole inbox. Senders it sees often get a parser written automatically, and when a reviewer corrects something, LEO learns from it." |
| 4 | 0:20 | The findings table | "Five findings, $17,184 at stake. The first column counts days to the send-by date. Corvana's notice is due November 15, so LEO says send it by the 13th." |
| 5 | 0:30 | Open the Lumenpost finding, then click a number: the quote is highlighted in the email, then in the order form | "Every number links to the sentence it came from." |
| 6 | 0:20 | Approvals: the Lumenpost draft, to billing@lumenpost.example, citing account LP-204417. Approve it. | "LEO drafts the reply, and a person approves every message. Nothing goes out on its own." Sending is simulated, so say "approve", never "send to the vendor". |
| 7 | 0:15 | Vendors → Ferrolink: its escalator term and matched charges, with no finding | "It also knows when not to flag. Ferrolink raised its price 4% on the contract anniversary, which is exactly what the contract allows." (N1 is the correct twin of Halcyon's early escalator.) |
| 8 | 0:15 | A vendor page's matched charges | "Charges come in through a pluggable transaction source; here it's a CSV. A Ramp connection is the natural feed: reminders track end dates, and LEO checks every bill against the terms and the email trail. Next: pooling vendor spend across entities." |

Say once that the data is synthetic. Ramp is named only as a data source, with no logo or branding.

The feedback loop isn't shown on camera. Every term in this data is extracted correctly, so any
correction would be staged. Beat 3 covers it in one line.

## Dry-run measurements (2026-11-10)

- **Before:** 19 documents (13 emails, 6 contracts), all classified by Jev. 4 findings: Corvana $14,400 (11 days), Quillstack $1,512, Kestrel $576 (28 days), Halcyon $216 (30 days). 11 documents to Haiku, 3 to scripts (`kestrel-invoice` ×2, `quillstack-receipt`).
- **After the live email:** 20 documents, 5 findings, $17,184. Lumenpost: $480, 28 days. Its draft is from Haiku, not the template.
- **Timing:** about 14s from the sync to the finding: Jev about 1s, Haiku extraction about 12s, reconcile under 1s. Add up to 10s for the Gmail watch and 5s for the dashboard poll.
- **Jev** labels the Quillstack email a receipt (its eval did too). F4 still appears.

## Cost

About $0.021 for the run: Claude Haiku 5.5 at $0.10/M input, $0.50/M output, cache writes at
1.25× input and cache reads at 0.1× input.

- **Extraction:** 36 calls (12 documents × 3 parts), matched to their cache entries by request id: 23,730 input, 24,169 output, 29,346 cache-write and 146,730 cache-read tokens, so $0.0196.
- **Drafts:** 5, at the average of the 12 draft cache entries ($0.00022 each), so $0.0011.

Jev's classification cost isn't included. Cache hits record no `model_usage`, so the dashboard
can't show this figure; it is said in the voiceover.
