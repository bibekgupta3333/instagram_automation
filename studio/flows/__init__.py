"""Prefect 3 orchestration (optional). Importable without Prefect; see README.md."""

from studio.flows.daily import (
    PREFECT_AVAILABLE,
    PREFECT_MISSING,
    daily_text_flow,
    overnight_gpu_flow,
)

__all__ = ["PREFECT_AVAILABLE", "PREFECT_MISSING", "daily_text_flow", "overnight_gpu_flow"]
