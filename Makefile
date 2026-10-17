.PHONY: install lint fmt typecheck test check-contracts check-schema ci land site

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

# Exports the newest validated Scene to site/data (needs the stack's STORE_* and PIPELINE_DATABASE_URL in the env), then
# serve it: cd site && python3 -m http.server 8099
site:
	uv run python -m pipeline.site_export

ci: lint typecheck test check-schema

# Builds data/land/great-belt.geojson from OSM land polygons (downloads ~0.9 GB once; needs the `land` extra).
land:
	uv run --extra land python -m pipeline.landpolygons --region contracts/region.greatbelt.yaml --out data/land/great-belt.geojson
