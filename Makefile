PYTHON ?= python

.PHONY: setup lint typecheck test run-api run-worker migrate demo

setup:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check .

typecheck:
	$(PYTHON) -m mypy app tests

test:
	$(PYTHON) -m pytest

run-api:
	$(PYTHON) -m uvicorn app.main:app --reload

run-worker:
	$(PYTHON) -m app.workers.runner

migrate:
	@echo "Apply migrations/0001_schema.sql then migrations/0002_functions.sql in Supabase SQL Editor."

demo:
	$(PYTHON) scripts/demo.py

