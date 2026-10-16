"""Pipeline stages. Each module exposes `run(job, brand, ctx) -> job` and is idempotent:
it skips when its cache key (<workdir>/.cache/<stage>.json) matches and its outputs exist.

Order: script -> fact_check -> tts -> captions -> images -> render.
"""
