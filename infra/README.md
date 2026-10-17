# infra

| Path | What it is |
|---|---|
| `litellm/config.yaml` | LiteLLM proxy: one OpenAI-compatible endpoint for Ollama (`local-bulk`) and Bedrock (`bedrock-haiku`, `bedrock-sonnet`, `bedrock-opus`). Follows docs 15 §7.4 |
| `MCP.md` | Notes for the root `.mcp.json` (operator console). JSON can't hold comments, so they live here |

## Run the gateway: `make gateway`

```bash
make ollama && ollama pull qwen3.5:9b  # local workhorse (docs 14)
export LITELLM_MASTER_KEY=sk-local-$(openssl rand -hex 8)
export OLLAMA_BASE_URL=http://localhost:11434
export AWS_REGION=us-east-1
export AWS_PROFILE=<your-bedrock-invoke-only-profile>   # or leave EMPTY: default AWS chain
make gateway        # root Makefile: uvx --from 'litellm[proxy]' litellm --config infra/litellm/config.yaml
curl -s http://127.0.0.1:4000/health/liveliness   # uvx downloads litellm[proxy] on first run: check free disk
```

Then point the studio at it:

```bash
export LITELLM_BASE_URL=http://127.0.0.1:4000
export LITELLM_API_KEY=<a per-channel virtual key, or the master key in dev>
```

`studio.llm.get_client()` chooses its backend in this order:

1. `STUDIO_LLM=mock`: an offline mock.
2. `LITELLM_BASE_URL`: the gateway, with the budget guard on.
3. `OLLAMA_BASE_URL`: Ollama directly (default `http://localhost:11434`). This serves **local
   roles only**. Quality roles raise an error rather than run locally. `STUDIO_LLM_LOCAL_QUALITY=1`
   is a dev-only override.

## Bedrock model IDs (text models only)

Bedrock serves **text** roles only here (no image or video models). Fill the IDs in the
git-ignored `.env`; an empty value means the built-in default:

| Variable | Roles (Haiku-tier today) | Default |
|---|---|---|
| `BEDROCK_MODEL_ID_FAST` | ideation, outline, draft, metadata, titles, polish_short, fact_check_light, ... | `global.anthropic.claude-haiku-5-5` |
| `BEDROCK_MODEL_ID_QUALITY` | polish, fact_check, title_rank, qa_judge_ymyl | `global.anthropic.claude-sonnet-5-5` |
| `BEDROCK_MODEL_ID_ESCALATION` | disputed fact-checks / polish escalation | `global.anthropic.claude-opus-5-5` |

Precedence in the direct client (`STUDIO_LLM=bedrock`): per-role `STUDIO_BEDROCK_MODEL_<ROLE>` >
tier `BEDROCK_MODEL_ID_*` > the older `BEDROCK_{HAIKU,SONNET,OPUS}_MODEL_ID` > default. Any id
form works: base id (`anthropic.claude-...`, `amazon.nova-lite-v1:0`), cross-Region profile
(`us.` / `eu.` / `apac.` / `global.`) or a full inference-profile ARN.

Budget pricing for your ids (`studio/llm/budget.py`): Claude ids price as their tier; known
non-Claude families (Nova, Llama, Mistral, ...) price at a conservative Claude-tier ceiling;
unknown ids price as the variable's tier with a one-time warning. For exact prices set
`BEDROCK_PRICE_IN_PER_MTOK_<TIER>` / `BEDROCK_PRICE_OUT_PER_MTOK_<TIER>` (USD per 1M tokens,
`<TIER>` = `FAST` / `QUALITY` / `ESCALATION`).

Non-Claude models: Anthropic-only fields (effort) are not sent, and models without Converse
tool use get JSON by prompt plus local schema validation (`STUDIO_BEDROCK_TOOL_USE=off|on`
forces it).

**Gateway (`STUDIO_LLM=gateway`):** LiteLLM's `os.environ/VAR` replaces a whole value and has
no default or interpolation (LiteLLM 1.101.0 `get_secret`); an empty var becomes `""`. So
`litellm/config.yaml` keeps the Claude defaults literally. After you fill a tier variable,
uncomment its `model_id: os.environ/BEDROCK_MODEL_ID_<TIER>` line (LiteLLM sends it as the
Converse `modelId`). For a non-Claude model, also set `model: bedrock/converse/<id>` so LiteLLM
uses that family's parameter mapping. Per-role overrides don't apply in gateway mode (it
routes by model group).

## Rules

- **No credentials in config.** Bedrock auth goes through the standard AWS chain
  (`AWS_PROFILE` / SSO). Scope IAM to `bedrock:InvokeModel*`/`Converse*` on the model and
  inference-profile ARNs, and add an AWS Budgets alert at 80% and 100% (docs 15 §7.2).
- Bind to `127.0.0.1`. Never expose Ollama or LiteLLM on `0.0.0.0` (docs 14).
- Budgets are layered:
  1. The app guard (`studio/llm/budget.py`: monthly cap, per-channel cap and shared pool, TPM
     buckets).
  2. LiteLLM `max_budget`, plus per-channel virtual keys once `database_url` is set.
  3. AWS Budgets.
- Quality roles queue (`BudgetDeferred`) when Bedrock is throttled or over budget. They never
  fall back to local. Only `local-bulk` falls back, to `bedrock-haiku`.

## Unverified

- The Bedrock `global.anthropic.claude-*-5-5` inference-profile IDs, from docs 15 §2. Check
  them with `aws bedrock list-inference-profiles`.
- Whether `litellm_settings.max_budget`/`budget_duration` is honoured as a proxy-wide cap on
  your LiteLLM version.
- How LiteLLM handles an empty `AWS_PROFILE` (`os.environ/AWS_PROFILE`). If it fails, remove
  `aws_profile_name` to use the default credential chain.
- The `tpm` values are placeholders. Set them to about 80% of the applied quota (docs 15 §3).
