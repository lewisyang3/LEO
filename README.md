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
| `seed` | Placeholder until ROADMAP A7 (exits 1) |
| `eval` | Score the classifier, extractor, validator and drafter against the answer key (`python -m leo.eval --help`). The held-out classifier set runs with `python -m leo.eval.holdout`. No database needed. The default fixture engines need no API keys |
| `e2e` | Placeholder until ROADMAP 2.2 (exits 1) |

## Layout

| Path | Contents | Track |
| --- | --- | --- |
| `leo/config.py` | Settings from the environment and `.env` | Shared |
| `leo/interfaces.py` | Stage protocols and the models passed between tracks | Shared |
| `leo/stubs.py` | Placeholder stage implementations until the tracks plug in | Shared |
| `leo/clock.py` | `Clock`: `LEO_TODAY`, or the system date | Shared |
| `leo/fixtures.py` | The answer key as typed fixtures, plus fixture-backed stages | Shared |
| `leo/db/` | Engine, models, Alembic migrations (`leo/db/migrations/`), repositories | A |
| `leo/ingest/` | Raw-file store, `.eml` parser, `ingest()` and folder loaders. Gmail sync later (A8) | A |
| `leo/pipeline/` | Classify, extract, validate | B |
| `leo/eval/` | Eval harness: scores stage output against `expected.json` | B |
| `leo/actions/` | Action drafting | B |
| `leo/reconcile/` | Reconciliation rules and deadlines | C |
| `leo/api/` | FastAPI app: findings, documents and the approval queue, on the fixture until C3 | C |
| `web/` | React + Vite + TypeScript UI | C |
| `tests/` | pytest | All |
| `scripts/validate_answer_key.py` | Checks `expected.json` against the dataset | All |
