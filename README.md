# LEO

LEO reads a business's vendor contracts, emails and invoices, records the terms they agree to (each with the quote it came from), and checks the transactions against those terms. It flags renewal deadlines, price increases, cap breaches, seat overages and early escalators, then drafts a fix for each one. Nothing is sent without human approval.

- [`ROADMAP.md`](ROADMAP.md): the tasks, the tracks and who owns them
- [`docs/STACK.md`](docs/STACK.md): the stack and the LLM policy
- [`data/synthetic/`](data/synthetic/): the Brightwater demo dataset and its answer key (`expected.json`)

## Prerequisites

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- Docker with Compose v2 (Docker Desktop must be running)
- Node 22 or later, with npm

## Quick start

```sh
make setup     # install Python and web dependencies, create .env from .env.example
make up        # start Postgres 16 and wait until it's healthy
make migrate   # apply Alembic migrations
make test      # run pytest (includes the answer-key validator)
make seed      # load the Brightwater demo tenant (the API and UI read it)
make reset     # later: wipe the database and reseed, for a clean demo
```

Then, in two terminals:

```sh
make api       # FastAPI on http://localhost:8000 (check /api/health)
make web       # Vite dev server; it proxies /api to the FastAPI app
```

Put your `ANTHROPIC_API_KEY` and `JEV_API_KEY` in `.env`. Never commit `.env`. The tests don't need either key. Ingested raw files go to `LEO_RAW_DIR` (default `var/raw`, gitignored). Tests use a temp folder instead.

## Make targets

| Target | Does |
| --- | --- |
| `setup` | `uv sync`, create `.env` if missing, `npm install` in `web/` |
| `up` / `down` | Start / stop Postgres (`docker compose`) |
| `migration` | Autogenerate a migration from the models: `make migration msg="what changed"`. Add extensions by hand |
| `migrate` | `alembic upgrade head` |
| `test` | `pytest` (needs `make up` and `make migrate`) |
| `lint` | `ruff check` and `ruff format --check` |
| `api` | Run FastAPI with reload |
| `web` | Run the Vite dev server |
| `seed` | Load the answer key into the database as the demo tenant (`python -m leo.db.seed`). A reseed keeps approvals and edits |
| `reset` | Drop the schema, migrate and seed, for a clean demo (`python -m leo.db.reset`). Stop the API and worker first. Refuses a non-local database without `--force` |
| `eval` | Score the classifier, extractor, validator and drafter against the answer key (`python -m leo.eval --help`). The held-out classifier set runs with `python -m leo.eval.holdout`. No database needed. The default fixture engines need no API keys |
| `e2e` | Placeholder until ROADMAP 2.2 (exits 1) |
| `gmail-connect` | Connect a Gmail account, read-only (opens the browser). See [Gmail sync](#gmail-sync-optional) |
| `gmail-sync` | Sync every connected account once, or `ARGS="--watch 10"` to poll |
| `gmail-import` | Import `.eml` files into the demo Gmail account (`scripts/seed_gmail.py`; `ARGS="--drip 5"` or `ARGS="--only FILE"`) |
| `api-gmail` | `make api` serving the Gmail tenant (`LEO_GMAIL_TENANT`) instead of the seeded one |

## Gmail sync (optional)

The demo never depends on this: the `.eml` path (`make seed`, uploads) is the fallback. Sync reads the inbox with the `gmail.readonly` scope and stores each new email through `ingest()`, which queues its `classify` job. Its tokens are encrypted at rest. It writes to its own tenant (`gmail-demo`), so it never doubles the seeded one.

One-time setup:

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable the Gmail API.
2. Configure the OAuth consent screen as **External**, in **Testing** mode, and add the demo Gmail account as a test user.
3. Create an OAuth client of type **Desktop app**, download its JSON to `var/google_client_secret.json` (gitignored), or point `LEO_GOOGLE_CLIENT_SECRETS` at it.
4. Generate `LEO_SECRET_KEY` and put it in `.env`:
   `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

Then:

```sh
make gmail-connect                   # consent in the browser (read-only); stores the account
make gmail-import ARGS="--drip 5"    # fill the demo inbox from data/synthetic/inbox (asks its own insert consent)
make gmail-sync ARGS="--watch 10"    # poll the inbox; each new email is a document with a classify job
make worker                          # run the jobs
make api-gmail                       # the API on the Gmail tenant
```

- `gmail-import` isn't idempotent: a second run imports every email again under new Gmail ids, so use a fresh demo account or delete the emails first. Its insert token is never stored.
- `make reset` drops the connected account and its token, so run `make gmail-connect` again afterwards.
- Don't put `LEO_TENANT=gmail-demo` in `.env`: `make seed` and `make reset` read it too, and would seed the answer key into the Gmail tenant. `make api-gmail` sets it for the API only.

## Layout

| Path | Contents | Track |
| --- | --- | --- |
| `leo/config.py` | Settings from the environment and `.env` | Shared |
| `leo/interfaces.py` | Stage protocols and the models passed between tracks | Shared |
| `leo/stubs.py` | Placeholder stage implementations until the tracks plug in | Shared |
| `leo/clock.py` | `Clock`: `LEO_TODAY`, or the system date | Shared |
| `leo/fixtures.py` | The answer key as typed fixtures, plus fixture-backed stages | Shared |
| `leo/db/` | Engine, models, Alembic migrations (`leo/db/migrations/`), repositories | A |
| `leo/ingest/` | Raw-file store, `.eml` parser, `ingest()`, folder loaders, Gmail sync (A8) and token encryption | A |
| `leo/pipeline/` | Classify, extract, validate | B |
| `leo/eval/` | Eval harness: scores stage output against `expected.json` | B |
| `leo/actions/` | Action drafting | B |
| `leo/reconcile/` | Reconciliation rules and deadlines | C |
| `leo/api/` | FastAPI app on Postgres: findings, documents, the approval queue, upload and stats (C3), CSV export (C4), and the vendor view (C5) | C |
| `web/` | React + Vite + TypeScript UI | C |
| `tests/` | pytest | All |
| `scripts/validate_answer_key.py` | Checks `expected.json` against the dataset | All |
| `scripts/seed_gmail.py` | Imports `.eml` files into the demo Gmail account (A8) | A |
