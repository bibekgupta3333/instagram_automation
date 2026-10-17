# LLM costs at full scale (all text on Bedrock)

As of 2026-10-16. Every text role now runs on Amazon Bedrock (`STUDIO_LLM=bedrock`): bulk roles on Haiku 5.5, quality roles on Sonnet 5.5, and disputes on Opus 5.5. Local LLMs are opt-in only. Prices are the global-profile rates from doc 15 §2, in USD per 1M tokens: Haiku $0.10 in / $0.50 out, Sonnet $2 / $10, Opus $4 / $20. Token counts per step come from doc 15 §5. Everything below is an *estimate*.

Scale: 300 Shorts and 80 long-form videos a month.

## Bulk roles on Haiku 5.5 (new spend; these were local before)

| Step (tokens in / out) | Short | Long-form |
|---|---|---|
| Ideation (8K / 3K) | $0.0023 | $0.0023 |
| Outline (4K / 2K) | $0.0014 | $0.0014 |
| Draft (Short 4K / 1K; long 15K / 4K) | $0.0009 | $0.0035 |
| Metadata (3K / 1K) | $0.0008 | $0.0008 |
| Title variants, thumbnail text, claim extraction (3K / 1K) | $0.0008 | $0.0008 |
| **Per video** | **$0.0062** | **$0.0088** |
| **Per month** | 300 × = **$1.86** | 80 × = **$0.70** |

**Bulk total: about $2.56 a month.** Doc 15 §5 gave about $2.60 for the same overflow case.

## Quality roles (unchanged, doc 15 §6 $25 tier)

| Line | Monthly |
|---|---|
| Long-form polish (Sonnet 5.5) | $5.20 |
| Shorts polish (Haiku 5.5) | $0.42 |
| Accuracy-critical fact-check + Opus 5.5 escalation | $7.40 |
| Other fact-checks (Haiku 5.5, long-form) | $0.22 |
| Long-form titles and hooks (Sonnet 5.5) | $1.68 |
| Comment mining, translation QA, Guardrails | $1.30 |
| **Quality total** | **$16.22** |

## Total

**$2.56 + $16.22 = $18.78 a month. With a 1.25× allowance for retries and regenerations, that is about $23.50 a month.**

Bulk work adds about $3.20 a month after the allowance. The defaults in `studio/llm/budget.py` and `infra/litellm/config.yaml` therefore went from $25 to **$30 a month**: $2.50 per channel × 10, plus a $5 shared pool. Override them with `STUDIO_BEDROCK_MONTHLY_USD`, `STUDIO_BEDROCK_CHANNEL_USD` and `STUDIO_BEDROCK_SHARED_POOL_USD`.

Watch out for these:
- Haiku 5.5 prompts over 100K tokens are billed at **5×**. The ledger applies that multiplier.
- Adaptive thinking is on by default for Claude 5.5 and is billed as output. The token figures assume effort `low`.
- `max_tokens` is reserved against TPM up front, so keep it tight.

## Claude Code alternative (`STUDIO_LLM=claude-code`)

This backend has no Bedrock bill. Calls run on the user's Claude subscription limits, the same allowance used for coding. Claude Code reports a notional `total_cost_usd`: the one-call smoke test (Haiku, `{"ok":true}`) showed **$0.00025**. Running 380 videos a month through it would use a noticeable share of a Pro or Max plan's 5-hour and weekly limits. When a limit is hit, the call is queued with `BudgetDeferred`.

## Your own model IDs (`BEDROCK_MODEL_ID_FAST` / `_QUALITY` / `_ESCALATION`)

The figures above assume the Claude 5.5 defaults. The budget guard prices a user-supplied id by its text: Claude ids price as Haiku, Sonnet or Opus 5.5. Known non-Claude families price at a conservative Claude-tier ceiling (*est.*). Nova Micro, Nova Lite and Ministral use Haiku prices. Nova Pro and Premier, Llama, Mistral and Mixtral, DeepSeek, Qwen, gpt-oss, Jamba, Gemma, Nemotron and Command R use Sonnet prices. Command R+ uses Opus prices. Any other id prices as its variable's tier (FAST → Haiku, QUALITY → Sonnet, ESCALATION → Opus), with a one-time warning. For exact numbers, set `BEDROCK_PRICE_IN_PER_MTOK_<TIER>` and `BEDROCK_PRICE_OUT_PER_MTOK_<TIER>` in USD per 1M tokens. Cache read and write are then priced at 0.1× and 1.25× the input price.
