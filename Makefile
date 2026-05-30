.PHONY: venv up down logs test lint loadtest locust reconcile chaos-worker chaos-redis

PY ?= .venv/bin/python
USERS ?= 50000
STOCK ?= 1000
CAMPAIGN ?= demo

venv:
	python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev,loadtest]"

up:
	docker compose up -d --build api worker

down:
	docker compose down -v

logs:
	docker compose logs -f api worker

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check .

loadtest:
	$(PY) -m loadtest.correctness --users $(USERS) --stock $(STOCK)

locust:
	$(PY) -m loadtest.create_campaign $(CAMPAIGN) --stock $(STOCK)
	CAMPAIGN_ID=$(CAMPAIGN) .venv/bin/locust -f loadtest/locustfile.py --host http://localhost:8000

reconcile:
	docker compose run --rm reconciler python -m reconciler.main

chaos-worker:
	PATH=.venv/bin:$$PATH bash loadtest/chaos/kill_worker.sh

chaos-redis:
	$(PY) -m loadtest.chaos.simulate_redis_loss --campaign $(CAMPAIGN)

.PHONY: smoke openapi postman
smoke:
	$(PY) -m scripts.smoke

openapi:
	$(PY) -m scripts.export_openapi

postman:
	$(PY) -m scripts.build_postman
