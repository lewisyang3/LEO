.PHONY: setup up down migrate test lint api web seed eval e2e

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

web:
	cd web && npm run dev

seed:
	uv run python -m leo.db.seed

eval:
	uv run python -m leo.eval

e2e:
	@echo "make e2e: not implemented yet, see ROADMAP 2.2" >&2; exit 1
