# pipeline-api image. Build from the repository root:
#   docker build -f deploy/pipeline-api/Dockerfile -t sar-pipeline-api .
# Settings come from PIPELINE_* variables (PIPELINE_DATABASE_URL is required); see src/pipeline/api/config.py.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first so this layer is cached until the lock file changes.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project --extra land

COPY src ./src
COPY contracts ./contracts
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
# Non-editable: only /app/.venv is copied to the final image, so the package must live inside it.
RUN uv sync --frozen --no-dev --no-editable --extra land


FROM python:3.12-slim
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/contracts /app/contracts
COPY --from=build /app/alembic.ini /app/alembic.ini
COPY --from=build /app/migrations /app/migrations
# Writable data directory (land polygons); a named volume mounted here inherits this ownership
RUN mkdir -p /app/data && chown app /app/data
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/v1/healthz', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "--factory", "pipeline.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
