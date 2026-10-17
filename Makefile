.PHONY: install lint fmt typecheck test check-contracts check-schema ci

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest

check-contracts:
	uv run pytest tests/contracts

# Applies contracts/schema.sql to a throwaway PostGIS container (needs Docker).
check-schema:
	scripts/check_schema.sh

ci: lint typecheck test check-schema
