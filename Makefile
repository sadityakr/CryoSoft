# CryoSoft development checks — single source of truth for all quality gates.
#
# CI (GitHub Actions now, GitLab CI after the migration) calls these targets;
# keep all check logic HERE so the CI configs stay thin wrappers that never
# need to change when a check is added or adjusted.
#
# Usage (from an activated .venv, or any environment with the dev deps):
#   make check       run every blocking gate (lint + contracts + tests)
#   make test        run the pytest suite (hardware-marked tests excluded)
#   make contracts   verify the layer import contracts (import-linter)
#   make lint        ruff error-level lint (undefined names, unused imports)
#   make typecheck   mypy basic mode — advisory, not part of `check` yet
#   make install     editable install with dev dependencies
#
# Windows note: install GNU make once via `scoop install make`. Every target
# is a single command that can also be run directly without make.

PYTHON ?= python

.PHONY: install test contracts lint typecheck check

install:
	$(PYTHON) -m pip install -e .[dev]

test:
	$(PYTHON) -m pytest -m "not hardware"

contracts:
	lint-imports

lint:
	ruff check .

typecheck:
	-$(PYTHON) -m mypy

check: lint contracts test
	@echo "All blocking checks passed."
