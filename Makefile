VENV := .venv/bin

.PHONY: help setup lint test test-cov test-all test-pio act clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

setup: ## Set up dev environment (venv, deps, git hooks)
	./scripts/dev-setup.sh

lint: ## Run ruff lint + format check + mypy
	$(VENV)/ruff check .
	$(VENV)/ruff format --check .
	$(VENV)/mypy pio_lock.py

format: ## Auto-fix lint and format issues
	$(VENV)/ruff check --fix .
	$(VENV)/ruff format .

test: ## Run unit tests (fast, no PIO needed)
	$(VENV)/python -m pytest -m "not integration" -v

test-cov: ## Run unit tests with coverage report
	$(VENV)/python -m pytest -m "not integration" -v --cov=pio_lock --cov-report=term-missing --cov-report=html:htmlcov

test-all: ## Run all tests including integration (needs PIO)
	$(VENV)/python -m pytest -v

test-pio: ## Run integration tests on one PlatformIO version in isolated dirs (make test-pio PIO=6.2.0)
	@test -n "$(PIO)" || { echo "usage: make test-pio PIO=<platformio version>"; exit 2; }
	python3 -m venv .venv-pio-$(PIO)
	.venv-pio-$(PIO)/bin/pip install -q "platformio==$(PIO)" pytest
	PLATFORMIO_CORE_DIR=$(CURDIR)/.pio-core-$(PIO) PATH=$(CURDIR)/.venv-pio-$(PIO)/bin:$$PATH \
		.venv-pio-$(PIO)/bin/python -m pytest -m integration -v

act: ## Run full CI locally via act
	act --container-architecture linux/amd64

clean: ## Remove build artifacts and caches
	rm -rf .venv .venv-pio-* .pio-core-* .mypy_cache .pytest_cache .ruff_cache __pycache__ tests/__pycache__ *.egg-info dist build htmlcov reports .coverage
