# One image for api / worker / reconciler; the command picks the role.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY common ./common
COPY api ./api
COPY worker ./worker
COPY reconciler ./reconciler
COPY loadtest ./loadtest
RUN pip install ".[loadtest]"

RUN useradd --uid 10001 --no-create-home app
USER app

EXPOSE 8000
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--no-access-log"]
