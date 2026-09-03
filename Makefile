.PHONY: dev test db-up db-down lint format install

install:
	poetry install

db-up:
	docker compose up -d postgres redis
	@echo "Waiting for services to be healthy..."
	@powershell -Command "Start-Sleep -Seconds 3"

db-down:
	docker compose down

dev:
	poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	poetry run pytest tests/ -v --tb=short

test-cov:
	poetry run pytest tests/ -v --tb=short --cov=app --cov-report=term-missing

lint:
	poetry run ruff check app/ tests/

format:
	poetry run ruff format app/ tests/

typecheck:
	poetry run mypy app/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
