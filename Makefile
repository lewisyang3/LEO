.PHONY: setup up down migrate test lint api api-gmail api-pipeline web seed reset ingest eval bar e2e scriptgen worker gmail-connect gmail-sync gmail-import demo-prep

setup:
	uv sync
	test -f .env || cp .env.example .env
	cd web && npm install

up:
	docker compose up -d --wait

down:
	docker compose down

migration:
	uv run alembic revision --autogenerate -m "$(msg)"

migrate:
	uv run alembic upgrade head

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

api:
	uv run uvicorn leo.api.app:app --reload

# The Gmail tenant for this process only: seed and reset also read LEO_TENANT.
api-gmail:
	LEO_TENANT=$${LEO_GMAIL_TENANT:-gmail-demo} uv run uvicorn leo.api.app:app --reload

# The pipeline's tenant: `make ingest`, then `make worker ARGS=--drain`.
api-pipeline:
	LEO_TENANT=$${LEO_PIPELINE_TENANT:-brightwater-pipeline} uv run uvicorn leo.api.app:app --reload

web:
	cd web && npm run dev

seed:
	uv run python -m leo.db.seed

reset:
	uv run python -m leo.db.reset

# Load the synthetic documents into the pipeline's tenant and queue them.
ingest:
	uv run python -m leo.ingest.folder $(ARGS)

worker:
	uv run python -m leo.worker $(ARGS)

gmail-connect:
	uv run python -m leo.ingest.gmail connect $(ARGS)

gmail-sync:
	uv run python -m leo.ingest.gmail sync $(ARGS)

gmail-import:
	uv run python scripts/seed_gmail.py $(ARGS)

# The demo video's "before" state: reset (which reseeds LEO_TENANT, never the
# Gmail tenant), the contracts as files, then Gmail connect (browser consent), sync and drain.
DEMO_TENANT = $${LEO_GMAIL_TENANT:-gmail-demo}
demo-prep:
	@if [ "$${LEO_TENANT}" = "$(DEMO_TENANT)" ]; then echo "LEO_TENANT must not be the Gmail tenant: reset would seed the answer key into it"; exit 1; fi
	uv run python -m leo.db.reset
	uv run python -m leo.ingest.folder --folder contracts --tenant $(DEMO_TENANT)
	uv run python -m leo.ingest.gmail connect
	uv run python -m leo.ingest.gmail sync
	uv run python -m leo.worker --drain --tenant $(DEMO_TENANT)

eval:
	uv run python -m leo.eval

# The accuracy bar's extraction half, offline from the committed caches. With make e2e.
bar:
	LEO_LLM_OFFLINE=1 uv run python -m leo.eval --extractor script+llm --bar

# Write scripts for formats the LLM keeps extracting. After make e2e.
scriptgen:
	uv run python -m leo.scriptgen $(ARGS)

# Reset the dev DB, run the pipeline on the synthetic set, check it. ARGS=--offline: no network.
e2e:
	uv run python -m leo.e2e $(ARGS)
