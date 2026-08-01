.PHONY: dev check fix test mutate e2e install backend frontend

install:
	cd backend && uv sync --all-groups
	cd frontend && npm install

dev:
	docker compose up --build

backend:
	cd backend && uv run uvicorn sellowl.main:app --reload --port 8000

frontend:
	cd frontend && npm run dev

check:
	cd backend && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest -q
	cd frontend && npx tsc -b --noEmit

fix:
	cd backend && uv run ruff format . && uv run ruff check --fix .

test:
	cd backend && uv run pytest -q

# Scoped to pricing.py + match.py. See docs/DEVELOP.md § Quality bar.
mutate:
	cd backend && rm -rf mutants && uv run mutmut run && uv run mutmut results

e2e:
	cd frontend && npx playwright test
