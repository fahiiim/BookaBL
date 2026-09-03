PYTHON ?= python

.PHONY: setup lint typecheck test run-api run-worker migrate demo

setup:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check app tests

typecheck:
	$(PYTHON) -m mypy app tests

test:
	$(PYTHON) -m pytest

run-api:
	$(PYTHON) -m uvicorn app.main:app --reload

run-worker:
	$(PYTHON) -m app.workers.runner

migrate:
	supabase db push

demo:
	$(PYTHON) scripts/demo.py
