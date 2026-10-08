# =============================================================
# geo-measure-api — Makefile
# =============================================================
# Targets:
#   install           Install all Python dependencies
#   run               Start the dev server with hot-reload
#   lint              Check style and formatting (ruff)
#   format            Auto-fix style and formatting (ruff)
#   test              Fast unit tests (no coverage)
#   test-cov          All tests with branch coverage report
#   test-unit         Unit tests only (no integration)
#   test-integration  Integration tests only (requires geo stack)
#   docker-build      Build the production Docker image
#   docker-up         Start services in the background
#   docker-down       Stop and remove containers
#   docker-logs       Tail container logs
#   docker-shell      Open a shell in the running container
#   clean             Remove caches and temporary artefacts

.PHONY: install run lint format \
        test test-cov test-unit test-integration \
        docker-build docker-up docker-down docker-logs docker-shell \
        clean

# ---------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------

install:
	pip install --upgrade pip
	pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Development server
# ---------------------------------------------------------------------------

run:
	uvicorn app.main:app \
	    --reload \
	    --host 0.0.0.0 \
	    --port 8000 \
	    --log-level debug

# ---------------------------------------------------------------------------
# Linting & formatting  (ruff config lives in pyproject.toml)
# ---------------------------------------------------------------------------

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

# Fast run — no coverage output (useful during TDD loops)
test:
	pytest -v --tb=short --no-cov

# Full suite with branch coverage, terminal + HTML reports
test-cov:
	pytest --tb=short

# Unit tests only (skips test_integration.py which needs GDAL)
test-unit:
	pytest tests/ \
	    --ignore=tests/test_integration.py \
	    --ignore=tests/conftest_integration.py \
	    -v --tb=short --no-cov

# Full-stack integration tests (requires GDAL + GeoPandas installed)
test-integration:
	pytest tests/test_integration.py -v --tb=short --no-cov

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

docker-build:
	docker compose build --no-cache

docker-up:
	docker compose up -d
	@echo "API running at http://localhost:$${HOST_PORT:-8000}"
	@echo "Docs:         http://localhost:$${HOST_PORT:-8000}/docs"

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f api

docker-shell:
	docker compose exec api /bin/bash

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete 2>/dev/null; true
	rm -rf htmlcov .coverage .pytest_cache
	rm -f geo_measure.db
