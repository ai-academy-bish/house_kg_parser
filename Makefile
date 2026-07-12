## house.kg — scraper & dataset builder
##
## Run `make help` for the command list.

SHELL := /bin/bash
.DEFAULT_GOAL := help

VENV    := venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
CONFIG  ?= config.yaml
LIMIT   ?=

# -- colours ----------------------------------------------------------------
BOLD   := \033[1m
DIM    := \033[2m
RESET  := \033[0m
CYAN   := \033[36m
GREEN  := \033[32m
YELLOW := \033[33m
BLUE   := \033[34m
MAGENTA:= \033[35m
RED    := \033[31m

define banner
	@printf "$(BOLD)$(CYAN)\n"
	@printf "  ┌─────────────────────────────────────────────┐\n"
	@printf "  │  house.kg  ·  %-30s│\n" "$(1)"
	@printf "  └─────────────────────────────────────────────┘\n"
	@printf "$(RESET)\n"
endef

.PHONY: help setup login parsing_run make_hf_dataset validate clean lint test

help: ## Show this help
	$(call banner,command reference)
	@printf "$(BOLD)Usage:$(RESET) make $(CYAN)<target>$(RESET) [$(DIM)VAR=value$(RESET)]\n\n"
	@printf "$(BOLD)Targets:$(RESET)\n"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  $(CYAN)%-18s$(RESET) %s\n", $$1, $$2}'
	@printf "\n$(BOLD)Variables:$(RESET)\n"
	@printf "  $(YELLOW)%-18s$(RESET) %s\n" "CONFIG" "path to the YAML config (default: config.yaml)"
	@printf "  $(YELLOW)%-18s$(RESET) %s\n" "LIMIT"  "stop after N listings (e.g. make parsing_run LIMIT=100)"
	@printf "\n$(BOLD)Typical flow:$(RESET)\n"
	@printf "  $(DIM)1.$(RESET) make $(GREEN)setup$(RESET)             $(DIM)# create venv, install deps$(RESET)\n"
	@printf "  $(DIM)2.$(RESET) make $(GREEN)login$(RESET)             $(DIM)# HuggingFace token (only if pushing)$(RESET)\n"
	@printf "  $(DIM)3.$(RESET) make $(GREEN)parsing_run$(RESET)       $(DIM)# scrape (resumable — safe to re-run)$(RESET)\n"
	@printf "  $(DIM)4.$(RESET) make $(GREEN)validate$(RESET)          $(DIM)# check keys, FKs, photos, prices$(RESET)\n"
	@printf "  $(DIM)5.$(RESET) make $(GREEN)make_hf_dataset$(RESET)   $(DIM)# build parquet subsets (+ push)$(RESET)\n\n"

setup: ## Create the virtualenv (uv) and install dependencies
	$(call banner,setup)
	@command -v uv >/dev/null 2>&1 || { \
		printf "$(YELLOW)uv not found — installing…$(RESET)\n"; \
		curl -LsSf https://astral.sh/uv/install.sh | sh; }
	@printf "$(BLUE)▸ creating virtualenv$(RESET) $(DIM)$(VENV)$(RESET)\n"
	@uv venv $(VENV)
	@printf "$(BLUE)▸ installing dependencies$(RESET)\n"
	@VIRTUAL_ENV=$(VENV) uv pip install -e ".[dev]"
	@printf "\n$(GREEN)$(BOLD)✓ ready$(RESET) — next: $(CYAN)make parsing_run$(RESET)\n\n"

login: ## Authenticate with HuggingFace (hf auth login)
	$(call banner,huggingface login)
	@printf "$(DIM)A token with write access is needed only to push the dataset.$(RESET)\n"
	@printf "$(DIM)Create one at https://huggingface.co/settings/tokens$(RESET)\n\n"
	@$(VENV)/bin/hf auth login
	@printf "\n$(GREEN)$(BOLD)✓ logged in$(RESET)\n\n"

parsing_run: ## Scrape house.kg (resumable; LIMIT=N for a smaller run)
	$(call banner,parsing run)
	@printf "$(DIM)config: $(CONFIG)$(RESET)\n"
	@printf "$(DIM)Interrupt safely with Ctrl-C — re-running resumes where it stopped.$(RESET)\n\n"
	@$(PY) -m house_kg.cli --config $(CONFIG) crawl $(if $(LIMIT),--limit $(LIMIT))
	@printf "\n$(GREEN)$(BOLD)✓ crawl finished$(RESET) — next: $(CYAN)make validate$(RESET)\n\n"

validate: ## Check primary keys, foreign keys, photos and price semantics
	$(call banner,validate)
	@$(PY) -m house_kg.cli --config $(CONFIG) validate

make_hf_dataset: ## Build the HuggingFace dataset (and push if configured)
	$(call banner,build hf dataset)
	@$(PY) -m house_kg.cli --config $(CONFIG) build
	@printf "\n$(GREEN)$(BOLD)✓ dataset built$(RESET)\n\n"

lint: ## Run ruff and mypy
	@$(VENV)/bin/ruff check house_kg
	@$(VENV)/bin/mypy house_kg || true

test: ## Run the test suite
	@$(VENV)/bin/pytest -q

clean: ## Remove scraped data, logs and the built dataset (keeps the venv)
	$(call banner,clean)
	@printf "$(RED)removing:$(RESET) data/ hf_dataset/ logs/\n"
	@rm -rf data hf_dataset logs
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@printf "$(GREEN)✓ clean$(RESET)\n\n"
