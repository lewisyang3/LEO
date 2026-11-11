# LEO

**LEO** is a project for the **Ramp x UC Berkeley Business Society** hackathon. The prompt:

> *Automate an outdated financial workflow using the newest AI tools.*

## The problem

Small businesses pay dozens of software and service vendors, but the terms that cost them money are scattered across PDFs, shared drives and inboxes. Contracts auto-renew, mid-term price escalators kick in, and "our rates are changing" emails get archived unread. Checking each bill against what was actually agreed is a manual job of spreadsheets and email searches, so it rarely happens. By the time the higher charge lands, the window to cancel or renegotiate has closed.

Renewal reminders exist, but they only track contract end dates. They miss price changes announced by email, and they can't tell you that a bill doesn't match the contract.

## How LEO solves it

LEO reads a business's vendor contracts, emails and invoices, keeps a record of every term the business agreed to (each with the exact quote it came from), and checks the card charges against those terms. It runs every document through a pipeline in which each stage is cheaper than the next, so most documents never reach the expensive one:

1. **Classify.** Jev (TypeSafe AI) labels each document (contract, order form, price notice, invoice, noise). Noise stops here.
2. **Extract.** Known formats go to deterministic scripts. Everything else goes to Claude, which returns structured terms with their source quotes.
3. **Validate.** Rules and Jev check the output: dates parse, amounts are plausible and every quote really appears in the document.
4. **Reconcile.** Vendor names are matched to messy card descriptors (`PAYWAVE* LUMENPOST`), and the terms are compared with the charges. LEO flags upcoming renewal deadlines, price increases, cap breaches, seat overages and early escalators, with the dollars at stake.
5. **Act.** Claude drafts the fix for each finding: a cancellation notice, a negotiation email or a dispute. **Nothing is sent without human approval.**

LEO also improves as it's used. When a reviewer corrects an extracted term, the correction becomes a few-shot example for future extractions. When the LLM keeps extracting from the same sender, Claude writes a script for that format, and the script is promoted only if it reproduces the verified results.

Emails arrive through a read-only Gmail sync, and documents can also be uploaded. The demo runs on Brightwater Coffee Co., a fictional three-location coffee shop with five planted issues ([`data/synthetic/`](data/synthetic/), answer key in `expected.json`).

## Stack

| Layer | Choice |
| --- | --- |
| Backend | Python 3.13, FastAPI, Pydantic v2, managed with [uv](https://docs.astral.sh/uv/) |
| Database | PostgreSQL 16 (Docker Compose), SQLAlchemy 2 and Alembic |
| Frontend | React, Vite and TypeScript |
| Classification and validation | Jev (TypeSafe AI) |
| LLM | Claude Haiku 5.5 through the `anthropic` SDK, for extraction, drafting and script generation, with structured outputs and a response cache |
| Inbox | Gmail API (read-only), with OAuth tokens encrypted at rest |
| Tests and lint | pytest and ruff |

## Quick start

You need Python 3.13 with [uv](https://docs.astral.sh/uv/), Docker with Compose v2 (Docker Desktop must be running), and Node 22 or later with npm.

```sh
make setup     # install Python and web dependencies, create .env from .env.example
make up        # start Postgres 16 and wait until it's healthy
make migrate   # apply the database migrations
make seed      # load the Brightwater demo data
```

Then, in two terminals:

```sh
make api       # FastAPI on http://localhost:8000 (check /api/health)
make web       # the dashboard on http://localhost:5173
```

Open http://localhost:5173 to see the findings, their sources and the drafted fixes waiting for approval.

`make seed` loads the answer key's results, so it needs no API keys. To have LEO find the issues itself, put your `ANTHROPIC_API_KEY` and `JEV_API_KEY` in `.env` (never commit it) and run the documents through the pipeline:

```sh
make ingest                                         # queue the 21 demo documents
make worker ARGS="--drain --tenant brightwater-pipeline"
make api-pipeline                                   # serve the pipeline's results, then make web
```

`make test` runs the test suite (no API keys needed), and `make reset` wipes the database and reseeds it for a clean demo.

More detail: [`docs/STACK.md`](docs/STACK.md) (the stack and the LLM policy), [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) and [`docs/DEMO.md`](docs/DEMO.md) (the demo video).
