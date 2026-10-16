# studio/flows: Prefect 3 orchestration

Skeleton of the two daily phases from docs 16 §4. Each flow calls
`studio.pipeline.runner.run_batch` (imported lazily; it's owned by the pipeline side).

| Flow | Schedule | What it does | Lane |
|---|---|---|---|
| `daily-text` | 08:00 | `run_batch(day, channels, phase="text", dry_run)`: scout, script, fact-check, package, QA. One local LLM stays loaded (`OLLAMA_KEEP_ALIVE=-1`) | `api` tag; Bedrock calls go through the budget guard |
| `overnight-gpu` | 18:00 | Unloads the local LLM (`keep_alive: 0`), then `run_batch(..., phase="render")`: TTS, alignment, visuals, render | `gpu` tag plus global concurrency limit `gpu` = **1** |

`call_run_batch` passes only the keyword arguments that `run_batch` accepts (`day`, `channels`,
`phase`, `dry_run`), so the flows keep working while that signature settles.

## Without Prefect

Prefect is an optional extra and isn't installed by default. `import studio.flows` still works:
the `@flow`/`@task` decorators become pass-throughs and the flows run as plain functions.
`studio.flows.PREFECT_MISSING` says what you lose: schedules, retries, caching and concurrency
limits.

```bash
uv run python -c "from studio.flows import daily_text_flow; daily_text_flow(dry_run=True)"
```

## With Prefect

```bash
uv sync --extra flows
prefect server start                       # local UI on :4200
prefect gcl create gpu --limit 1           # never two GPU jobs at once (24 GB unified memory)
prefect gcl create api --limit 8
prefect gcl create publish --limit 1       # Buffer: 100 req/15 min shared with MCP/CLI
prefect concurrency-limit create gpu 1     # tag-based limit for tasks tagged "gpu"
uv run python -m studio.flows.daily        # serve both deployments on their cron schedules
```

The flows never publish. Publishing runs only after a human approval and goes through
`studio.publish` (gate, then Buffer/YouTube). The `publish` lane is reserved for that step.
