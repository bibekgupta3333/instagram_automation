# Local studio setup and dev tasks. See SETUP.md.
SHELL := /bin/bash
SETUP := scripts/setup

# make models               default set = every model with `optional: false` in
#                           scripts/setup/models.manifest.yaml (Configuration A: Kokoro, parakeet,
#                           Z-Image-Turbo q4, Real-ESRGAN, Depth Anything V2 Small; ~9 GB).
#                           Text runs on Bedrock / Claude Code, so every llm model is optional.
# make models STAGE=tts     one stage (llm | tts | asr | image | video | music | all)
STAGE ?= all
# make models OPTIONAL=1  also pulls models marked optional (local LLMs, flux2-klein, video, ACE-Step)
OPTIONAL ?=
OPT_FLAG := $(if $(OPTIONAL),--include-optional,)
# Exact names (space separated); avoids downloading every optional video model.
MODEL ?=
MODEL_FLAGS := $(foreach name,$(MODEL),--name $(name))
# make smoke-mock ARGS="--channel demo"  extra args passed to `studio make --mock`
ARGS ?=

LITELLM_CONFIG ?= infra/litellm/config.yaml
LITELLM_PORT ?= 4000

# WARNING: `uv sync` uninstalls every extra you don't name. Always sync through `make deps`;
# a bare `uv sync` drops mflux/torch (local-image) and the audio stack. `uv run` is safe.
UV_EXTRAS := --extra local-audio --extra local-image --extra bedrock --group dev

.PHONY: help preflight tools tools-dry deps deps-audio models-dry models models-audio smoke smoke-mock \
	test lint gateway ollama deps-web web test-web

help: ## list targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

preflight: ## check hardware, disk, tools, ffmpeg, Ollama, venv (read-only)
	@bash $(SETUP)/00-preflight.sh

tools: ## install missing LIGHT tools via brew (idempotent)
	@bash $(SETUP)/10-install-tools.sh

tools-dry: ## show what `make tools` would install
	@bash $(SETUP)/10-install-tools.sh --dry-run

deps: ## install Configuration A runtimes: audio + image (mflux, torch, opencv, spandrel) + bedrock + dev
	@echo "uv sync $(UV_EXTRAS)   (uv sync drops unnamed extras: always sync via make deps)"
	uv sync $(UV_EXTRAS)

deps-audio: deps ## alias for `make deps` (a narrower sync would uninstall local-image)

models-dry: ## plan model downloads + disk check, download nothing
	@bash $(SETUP)/20-pull-models.sh --stage $(STAGE) --dry-run $(OPT_FLAG) $(MODEL_FLAGS)

models: ## download the manifest's default (non-optional) models (refuses if disk is short)
	@bash $(SETUP)/20-pull-models.sh --stage $(STAGE) $(OPT_FLAG) $(MODEL_FLAGS)

models-audio: ## download only the audio models: Kokoro TTS + parakeet ASR (~2.9 GB)
	@bash $(SETUP)/20-pull-models.sh --stage tts
	@bash $(SETUP)/20-pull-models.sh --stage asr

smoke: ## 5 s test clip through each stage; SKIPs stages whose models are missing
	@bash $(SETUP)/30-smoke-test.sh

smoke-mock: ## run `studio make --mock` (SKIPs if the CLI isn't ready)
	@bash $(SETUP)/30-smoke-test.sh --mock $(if $(ARGS),-- $(ARGS),)

guarded: ## macOS: run studio with 3 CPU threads, stop if memory leaves green or disk <10 GB
	.venv/bin/python scripts/run_guarded.py -- .venv/bin/studio $(ARGS)

test: ## run the test suite
	uv run pytest -q

deps-web: ## install the isolated web app dependencies; preserves existing model runtimes
	@test -x .venv-web/bin/python || uv venv .venv-web --python $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
	uv pip install --python .venv-web/bin/python --require-hashes -r infra/web/requirements.lock
	uv pip install --python .venv-web/bin/python --no-deps -e .
	uv pip install --python .venv-web/bin/python pytest

web: ## serve the local creator studio at http://127.0.0.1:8080
	.venv-web/bin/python -m studio.web --host 127.0.0.1 --port 8080

test-web: ## offline web/API/render/resource tests
	.venv-web/bin/python -m pytest -q tests/test_web_*.py tests/test_creator_*.py

lint: ## ruff
	uv run ruff check .

gateway: ## start the LiteLLM proxy on 127.0.0.1:$(LITELLM_PORT)
	@test -f $(LITELLM_CONFIG) || { echo "missing $(LITELLM_CONFIG) (written by the gateway workstream)"; exit 1; }
	uvx --from 'litellm[proxy]' litellm --config $(LITELLM_CONFIG) --host 127.0.0.1 --port $(LITELLM_PORT)

ollama: ## OPTIONAL (text runs on Bedrock / Claude Code): start the Ollama server on 127.0.0.1:11434 (one model at a time, 16k ctx)
	OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_KEEP_ALIVE=10m \
	OLLAMA_CONTEXT_LENGTH=16384 ollama serve
