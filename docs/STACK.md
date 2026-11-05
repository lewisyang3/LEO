# LEO Stack

This page records the decisions from ROADMAP task 0.1. To change any of them, edit this page in a PR that all three teammates (Lewis, Erik, Omar) approve.

## Tools

| Layer | Choice | Notes |
| --- | --- | --- |
| Language | Python 3.13 | `requires-python = ">=3.13"` |
| Packaging | [uv](https://docs.astral.sh/uv/) | `pyproject.toml` + `uv.lock`; `uv sync`, `uv run` |
| API | FastAPI + Uvicorn | JSON API only; the UI is a separate app |
| Types | Pydantic v2 | Shared types (0.3) and LLM structured output |
| Database | PostgreSQL 16 (`postgres:16` image) with `pg_trgm` | Run locally with Docker Compose |
| ORM and migrations | SQLAlchemy 2 + Alembic, psycopg 3 driver | |
| Tests and lint | pytest, ruff | |
| Classification and prior-term comparison | Jev (TypeSafe AI), `typesafe_sdk` | Behind the `Classifier` and `Validator` interfaces. The key is in `JEV_API_KEY`, and `leo.jev.jev_client` passes it to the SDK explicitly, because the SDK's own variable is `TYPESAFE_API_KEY` |
| LLM | `anthropic` (official Python SDK), `claude-haiku-5-5` | No other LLM providers |
| Frontend | React + Vite + TypeScript, npm, in `web/` | Node 22 or later; Vite's dev server proxies `/api` to FastAPI |
| Gmail (A8, [should]) | `google-api-python-client`, read-only scope | |

## What does what

| Pipeline step | Engine |
| --- | --- |
| 1. Classify (contract, order form, price notice, invoice, receipt, noise) | Jev |
| 2. Script extraction for known formats | Deterministic code |
| 3. LLM extraction of terms and invoice facts | Haiku 5.5 (`LEO_MODEL_EXTRACT`) |
| 4. Validate | Deterministic rules check that quotes appear in the source, dates parse and amounts are plausible. A rule comparison and Jev both compare the new extraction with prior terms, and either one can flag a conflict |
| 5. Reconcile against transactions | Code |
| 5. Draft the action (cancellation, negotiation, dispute) | Haiku 5.5 (`LEO_MODEL_DRAFT`) |

## LLM policy

- **Efficiency first.** Both Haiku jobs default to `claude-haiku-5-5` ($0.10 input / $0.50 output per million tokens, for prompts up to 100K tokens). Each job reads its model from its own environment variable: `LEO_MODEL_EXTRACT` or `LEO_MODEL_DRAFT`. Classification (B2) is Jev only.
- **Moving a job to a heavier model needs evidence.** Move a job only when `make eval` (B1) shows Haiku missing the accuracy bar for that job.
  - The next step up is `claude-sonnet-5-5` ($2 / $10), then `claude-opus-5-5` ($4 / $20).
  - Record each move in the table below, with the eval numbers before and after.
- **Request rules:**
  - Set `effort` explicitly. Haiku 5.5 defaults to `medium`. Start every job at `medium` and tune it with the eval.
  - Get structured data with structured outputs (`client.messages.parse` or `output_config.format` with the Pydantic models), not with a forced `tool_choice`.
    - Use `output_config.format` through `leo.llm.structured_call`, not `client.messages.parse`, which raises before you can read `stop_reason`.
    - A schema is capped at 16 nullable fields, 24 optional fields and a total grammar size (checked 2026-11-04). Over the cap, the API returns 400 "compiled grammar is too large". Split the output into parts, as B3 does.
  - Check `stop_reason` before reading the content, and handle `refusal`. Haiku 5.5 has no server-side fallback, so a refused item goes to `needs_review`.
  - Cache responses (B3) keyed by document hash, job, model and prompt version. Reruns then reproduce the same quotes and keys, and the demo can run offline.
- **Secrets.** `ANTHROPIC_API_KEY` and `JEV_API_KEY` live in `.env`, which is never committed. `.env.example` lists the variable names without values.

### Model changes

| Date | Job | From | To | Eval before | Eval after |
| --- | --- | --- | --- | --- | --- |
| — | — | — | — | — | — |

## Jev risk

The design doc lists the risks: access is early, rate limits are unknown, and its speed, cost and accuracy claims haven't been verified.

- **No LLM fallback.** Classification is Jev only. With no `JEV_API_KEY`, the Jev classifier stops at startup. A failed Jev call leaves that document unclassified for review, never noise.
- **Threshold.** Jev's P(noise) alone decides noise (B2). The threshold is chosen from the eval's sweep over the labeled set, to keep 100% recall on the planted issues with the lowest false-pass. It is 0.5, which leaves margin on both sides. Every label keeps Jev's probabilities, and the eval lists the noise-labeled documents for a spot check.

## Local environment (checked 2026-11-02)

Python 3.13.5, uv 0.11.16, Docker 28.3 with Compose v2.39, Node 23.11 with npm.
