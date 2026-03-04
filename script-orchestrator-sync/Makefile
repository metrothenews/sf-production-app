SHELL := /bin/bash
.DEFAULT_GOAL := smoke-dry

BASE_URL_DEFAULT := https://YOUR-SHEETS-JSON-GATEWAY.a.run.app
REPO_DIR := $(CURDIR)
COMPOSE ?= docker compose

.PHONY: venv healthcheck healthcheck-render bootstrap-render smoke smoke-dry render-smoke compile-config requeue-render run-batch docker-build docker-up docker-down docker-shell docker-run-batch

venv:
	@if [ ! -d .venv ]; then python3 -m venv .venv; fi
	@source .venv/bin/activate && pip install -r requirements.txt

healthcheck:
	@if [ -z "$$CONTROL_SHEET_ID" ]; then echo "ERROR: CONTROL_SHEET_ID is required."; exit 1; fi
	@set -euo pipefail; \
	$(MAKE) venv; \
	cd $(REPO_DIR); \
	source .venv/bin/activate; \
	export PYTHONPATH=src; \
	export SHEETS_JSON_BASE_URL="$${SHEETS_JSON_BASE_URL:-$(BASE_URL_DEFAULT)}"; \
	python3 scripts/healthcheck_control_sheet.py

healthcheck-render:
	@if [ -z "$$CONTROL_SHEET_ID" ]; then echo "ERROR: CONTROL_SHEET_ID is required."; exit 1; fi
	@set -euo pipefail; \
	$(MAKE) venv; \
	cd $(REPO_DIR); \
	source .venv/bin/activate; \
	export PYTHONPATH=src; \
	export SHEETS_JSON_BASE_URL="$${SHEETS_JSON_BASE_URL:-$(BASE_URL_DEFAULT)}"; \
	REQUIRE_RENDER_TABS=1 python3 scripts/healthcheck_control_sheet.py

bootstrap-render:
	@if [ -z "$$CONTROL_SHEET_ID" ]; then echo "ERROR: CONTROL_SHEET_ID is required."; exit 1; fi
	@set -euo pipefail; \
	$(MAKE) venv; \
	cd $(REPO_DIR); \
	source .venv/bin/activate; \
	export PYTHONPATH=src; \
	export SHEETS_JSON_BASE_URL="$${SHEETS_JSON_BASE_URL:-$(BASE_URL_DEFAULT)}"; \
	python3 scripts/bootstrap_render_tabs.py

smoke-dry:
	@if [ -z "$$CONTROL_SHEET_ID" ]; then echo "ERROR: CONTROL_SHEET_ID is required."; exit 1; fi
	@set -euo pipefail; \
	$(MAKE) venv; \
	cd $(REPO_DIR); \
	source .venv/bin/activate; \
	export PYTHONPATH=src; \
	export SHEETS_JSON_BASE_URL="$${SHEETS_JSON_BASE_URL:-$(BASE_URL_DEFAULT)}"; \
	unset JOB_ID; \
	python3 scripts/bootstrap_control_sheet.py; \
	DRY_RUN=1 python3 scripts/smoke_e2e.py

smoke:
	@if [ -z "$$CONTROL_SHEET_ID" ]; then echo "ERROR: CONTROL_SHEET_ID is required."; exit 1; fi
	@set -euo pipefail; \
	$(MAKE) venv; \
	cd $(REPO_DIR); \
	source .venv/bin/activate; \
	export PYTHONPATH=src; \
	export SHEETS_JSON_BASE_URL="$${SHEETS_JSON_BASE_URL:-$(BASE_URL_DEFAULT)}"; \
	unset JOB_ID; \
	python3 scripts/bootstrap_control_sheet.py; \
	DRY_RUN=1 python3 scripts/smoke_e2e.py; \
	DEDUPE_KEY=make_smoke_live python3 scripts/smoke_e2e.py; \
	DEDUPE_KEY=make_smoke_live IDEMPOTENCY_VERIFY=1 python3 scripts/smoke_e2e.py

render-smoke:
	@if [ -z "$$CONTROL_SHEET_ID" ]; then echo "ERROR: CONTROL_SHEET_ID is required."; exit 1; fi
	@set -euo pipefail; \
	$(MAKE) bootstrap-render; \
	cd $(REPO_DIR); \
	source .venv/bin/activate; \
	export PYTHONPATH=src; \
	export SHEETS_JSON_BASE_URL="$${SHEETS_JSON_BASE_URL:-$(BASE_URL_DEFAULT)}"; \
	python3 scripts/render_agent.py run-once

compile-config:
	@if [ -z "$$CONTROL_SHEET_ID" ]; then echo "ERROR: CONTROL_SHEET_ID is required."; exit 1; fi
	@set -euo pipefail; \
	export CONFIG_SHEET_ID="$${CONFIG_SHEET_ID:-YOUR_GOOGLE_SHEET_ID}"; \
	export CONFIG_TAB="$${CONFIG_TAB:-Sheet1}"; \
	export CONFIG_RANGE="$${CONFIG_RANGE:-A1:AZ2}"; \
	export CLIPS_LIBRARY_FOLDER_ID="$${CLIPS_LIBRARY_FOLDER_ID:-YOUR_GOOGLE_SHEET_ID}"; \
	export OUTPUT_PARENT_FOLDER_ID="$${OUTPUT_PARENT_FOLDER_ID:-YOUR_GOOGLE_SHEET_ID}"; \
	export PYTHONPATH=src; \
	.venv_local/bin/python scripts/compile_config_to_render_job.py

requeue-render:
	@if [ -z "$$CONTROL_SHEET_ID" ]; then echo "ERROR: CONTROL_SHEET_ID is required."; exit 1; fi
	@if [ -z "$$RENDER_JOB_ID" ]; then echo "ERROR: RENDER_JOB_ID is required."; exit 1; fi
	@set -euo pipefail; \
	export PYTHONPATH=src; \
	.venv_local/bin/python scripts/requeue_render_job.py

run-batch:
	@if [ -z "$$CONTROL_SHEET_ID" ]; then echo "ERROR: CONTROL_SHEET_ID is required."; exit 1; fi
	@set -euo pipefail; \
	export CONFIG_SHEET_ID="$${CONFIG_SHEET_ID:-YOUR_GOOGLE_SHEET_ID}"; \
	export CONFIG_TAB="$${CONFIG_TAB:-Sheet1}"; \
	export CONFIG_RANGE="$${CONFIG_RANGE:-A1:AZ2}"; \
	export CLIPS_LIBRARY_FOLDER_ID="$${CLIPS_LIBRARY_FOLDER_ID:-YOUR_GOOGLE_SHEET_ID}"; \
	export OUTPUT_PARENT_FOLDER_ID="$${OUTPUT_PARENT_FOLDER_ID:-YOUR_GOOGLE_SHEET_ID}"; \
	export SHEETS_JSON_BASE_URL="$${SHEETS_JSON_BASE_URL:-$(BASE_URL_DEFAULT)}"; \
	export PYTHONPATH=src; \
	.venv_local/bin/python scripts/trigger_render_batch.py

docker-build:
	@$(COMPOSE) build app

docker-up:
	@$(COMPOSE) up app

docker-down:
	@$(COMPOSE) down

docker-shell:
	@$(COMPOSE) run --rm app bash

docker-run-batch:
	@if [ -z "$$CONTROL_SHEET_ID" ]; then echo "ERROR: CONTROL_SHEET_ID is required."; exit 1; fi
	@set -euo pipefail; \
	$(COMPOSE) run --rm app python scripts/trigger_render_batch.py
