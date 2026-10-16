# Local studio setup and dev tasks. See SETUP.md.
SHELL := /bin/bash
SETUP := scripts/setup

# make models STAGE=llm   (llm | tts | asr | image | music | all)
STAGE ?= all
# make models OPTIONAL=1  also pulls models marked optional (e.g. gemma4:12b)
OPTIONAL ?=
OPT_FLAG := $(if $(OPTIONAL),--include-optional,)
# make smoke-mock ARGS="--channel demo"  extra args passed to `studio make --mock`
ARGS ?=

LITELLM_CONFIG ?= infra/litellm/config.yaml
LITELLM_PORT ?= 4000

.PHONY: help preflight tools tools-dry models-dry models smoke smoke-mock test lint gateway ollama

help: ## list targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

preflight: ## check hardware, disk, tools, ffmpeg, Ollama, venv (read-only)
	@bash $(SETUP)/00-preflight.sh

tools: ## install missing LIGHT tools via brew (idempotent)
	@bash $(SETUP)/10-install-tools.sh

tools-dry: ## show what `make tools` would install
	@bash $(SETUP)/10-install-tools.sh --dry-run

models-dry: ## plan model downloads + disk check, download nothing
	@bash $(SETUP)/20-pull-models.sh --stage $(STAGE) --dry-run $(OPT_FLAG)

models: ## download model weights (refuses if disk is short)
	@bash $(SETUP)/20-pull-models.sh --stage $(STAGE) $(OPT_FLAG)

smoke: ## 5 s test clip through each stage; SKIPs stages whose models are missing
	@bash $(SETUP)/30-smoke-test.sh

smoke-mock: ## run `studio make --mock` (SKIPs if the CLI isn't ready)
	@bash $(SETUP)/30-smoke-test.sh --mock $(if $(ARGS),-- $(ARGS),)

test: ## run the test suite
	uv run pytest -q

lint: ## ruff
	uv run ruff check .

gateway: ## start the LiteLLM proxy on 127.0.0.1:$(LITELLM_PORT)
	@test -f $(LITELLM_CONFIG) || { echo "missing $(LITELLM_CONFIG) (written by the gateway workstream)"; exit 1; }
	uvx --from 'litellm[proxy]' litellm --config $(LITELLM_CONFIG) --host 127.0.0.1 --port $(LITELLM_PORT)

ollama: ## start the Ollama server on 127.0.0.1:11434 (one model at a time, 16k ctx)
	OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_KEEP_ALIVE=10m \
	OLLAMA_CONTEXT_LENGTH=16384 ollama serve
