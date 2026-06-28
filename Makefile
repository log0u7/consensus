# Makefile for the Consensus stack.
#
# Generic by default (reads .env). Layer an override with ENV=<name>:
#   - merges .env.<name> on top of .env (override wins)
#   - layers docker-compose.<name>.yml if it exists
# Toggle hot-reload dev mode with DEV=1.
#
# Examples:
#   make up                  # generic stack from .env
#   make up DEV=1            # hot reload
#   make logs
#   make run SPEC="write a Go HTTP server"

SHELL := /bin/bash

ENV ?=
DEV ?=
UI_URL := http://localhost:8800
ENV_ACTIVE := .env.active

# --- compose file layering -------------------------------------------------
COMPOSE_FILES := -f docker-compose.yml
ifneq ($(ENV),)
ifneq ($(wildcard docker-compose.$(ENV).yml),)
COMPOSE_FILES += -f docker-compose.$(ENV).yml
endif
endif
ifneq ($(DEV),)
COMPOSE_FILES += -f docker-compose.dev.yml
endif

DC := docker compose $(COMPOSE_FILES) --env-file $(ENV_ACTIVE)

# --- env merge -------------------------------------------------------------
.PHONY: _env
_env:
	@test -f .env || { echo 'missing .env (copy from .env.example and fill ZEN_API_KEY)'; exit 1; }
	@cat .env > $(ENV_ACTIVE)
	@if [ -n "$(ENV)" ]; then \
	  test -f .env.$(ENV) || { echo "missing .env.$(ENV)"; exit 1; }; \
	  echo '' >> $(ENV_ACTIVE); cat .env.$(ENV) >> $(ENV_ACTIVE); \
	fi

# --- targets ---------------------------------------------------------------
.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help and the active configuration
	@echo 'Consensus - make targets'
	@echo ''
	@echo 'Usage: make <target> [ENV=<name>] [DEV=1]'
	@echo ''
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ''
	@echo 'Active configuration:'
	@echo '  compose : $(COMPOSE_FILES)'
	@echo '  env     : .env$(if $(ENV), + .env.$(ENV),)'
	@echo '  dev     : $(if $(DEV),on,off)'
	@echo '  UI      : $(UI_URL)'

.PHONY: build
build: _env ## Build (or rebuild) the app image
	$(DC) build

.PHONY: up
up: _env ## Start the stack (build if needed), detached
	$(DC) up -d --build
	@echo 'UI: $(UI_URL)'

.PHONY: start
start: up ## Alias for 'up'

.PHONY: down
down: _env ## Stop and remove the stack
	$(DC) down

.PHONY: stop
stop: down ## Alias for 'down'

.PHONY: update
update: _env ## Rebuild and recreate the app after code changes
	$(DC) up -d --build --force-recreate app
	@echo 'UI: $(UI_URL)'

.PHONY: reload
reload: update ## Alias for 'update'

.PHONY: restart
restart: _env ## Restart the app container without rebuilding
	$(DC) restart app

.PHONY: logs
logs: _env ## Follow the app logs
	$(DC) logs -f app

.PHONY: ps
ps: _env ## Show stack status
	$(DC) ps

.PHONY: shell
shell: _env ## Open a shell in the app container
	$(DC) exec app /bin/bash

.PHONY: index
index: _env ## Index docs-projet into the RAG store
	$(DC) run --rm app python -m src.rag --index /app/docs-projet

.PHONY: run
run: _env ## Run the pipeline on the CLI: make run SPEC="..."
	$(DC) run --rm app python -m src.pipeline "$(SPEC)"

.PHONY: config
config: _env ## Show the merged compose configuration
	$(DC) config

.PHONY: nuke
nuke: _env ## Stop the stack and delete volumes (wipes pgvector data)
	$(DC) down -v

# --- Dev tooling (no .env / no provider needed) ----------------------------
# Tests are pure and offline. Dummy env vars let src.config import.
VENV := .venv-dev
PY := $(VENV)/bin/python
TEST_ENV := ZEN_API_KEY=dummy

.PHONY: dev-setup
dev-setup: ## Create the dev venv and install dev/test tooling
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PY) -m pip install -q --upgrade pip
	@$(PY) -m pip install -q -r requirements-dev.txt
	@echo 'dev env ready: $(VENV)'

.PHONY: tui
tui: _env ## Run the Textual TUI (stack must be up; interactive)
	$(DC) run --rm -it app python -m tui --api-url http://app:8000

.PHONY: lint
lint: dev-setup ## Lint with ruff
	$(VENV)/bin/ruff check src tests tui

.PHONY: format
format: dev-setup ## Auto-format with ruff
	$(VENV)/bin/ruff format src tests tui
	$(VENV)/bin/ruff check --fix src tests tui

.PHONY: typecheck
typecheck: dev-setup ## Static type check with mypy
	$(VENV)/bin/mypy src

.PHONY: test
test: dev-setup ## Run unit tests (local venv, offline)
	$(TEST_ENV) $(PY) -m pytest

.PHONY: check
check: lint typecheck test ## Lint + typecheck + test (the CI entrypoint)

.PHONY: test-docker
test-docker: ## Run lint+typecheck+test inside a container (iso CI env)
	docker build -t consensus-ci -f Dockerfile .
	docker run --rm -e ZEN_API_KEY=dummy \
	  -v "$(CURDIR)":/app -w /app consensus-ci \
	  sh -c "pip install -q -r requirements-dev.txt && ruff check src tests tui && mypy src tui && pytest"
