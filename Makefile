# Desktop is iCloud-synced, which breaks editable installs, so nothing here
# installs the project itself: tests and tools run against src/ on PYTHONPATH.

VENV := .venv
PY   := $(VENV)/bin/python
export PYTHONPATH := src

.PHONY: help setup demo test lint typecheck check up down logs psql clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## create the venv and install dependencies
	uv venv --python 3.12
	uv pip install --python $(VENV) --all-extras -r pyproject.toml

demo: ## run one investigation end to end, no infrastructure
	$(PY) -m faultline.cli demo --approve

test: ## run the test suite
	$(PY) -m pytest

lint: ## check formatting and lint rules
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests

typecheck: ## run mypy
	$(VENV)/bin/mypy

check: lint typecheck test ## everything CI runs

up: ## start Postgres and Redis
	docker compose up -d postgres redis

down: ## stop everything
	docker compose down

logs: ## follow service logs
	docker compose logs -f

psql: ## open a shell on the local database
	docker compose exec postgres psql -U faultline -d faultline

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage coverage.xml
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
