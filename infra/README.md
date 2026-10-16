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
