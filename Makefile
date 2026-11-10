.PHONY: setup up down migrate test lint api api-gmail web seed reset eval e2e worker gmail-connect gmail-sync gmail-import

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

web:
	cd web && npm run dev

seed:
	uv run python -m leo.db.seed

reset:
	uv run python -m leo.db.reset

worker:
	uv run python -m leo.worker

gmail-connect:
	uv run python -m leo.ingest.gmail connect $(ARGS)

gmail-sync:
	uv run python -m leo.ingest.gmail sync $(ARGS)

gmail-import:
	uv run python scripts/seed_gmail.py $(ARGS)

eval:
	uv run python -m leo.eval

e2e:
	@echo "make e2e: not implemented yet, see ROADMAP 2.2" >&2; exit 1
