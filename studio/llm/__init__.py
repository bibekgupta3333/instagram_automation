"""LLM layer: role routing (docs 15 §5), Bedrock Converse (default), Claude Code headless,
LiteLLM gateway / opt-in Ollama clients, and the budget guard."""

from studio.llm.budget import BudgetDeferred, BudgetGuard
from studio.llm.client import LLMError, LLMUnavailable, MockChatClient, get_client
from studio.llm.routing import resolve

__all__ = [
    "BudgetDeferred",
    "BudgetGuard",
    "LLMError",
    "LLMUnavailable",
    "MockChatClient",
    "get_client",
    "resolve",
]
