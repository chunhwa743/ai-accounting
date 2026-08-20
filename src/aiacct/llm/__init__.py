"""LLM access: the OpenAI client and its deterministic offline counterpart."""

from .client import LLMClient, LLMError, LLMResult, ModelRefusal, OpenAIClient, get_llm_client

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMResult",
    "ModelRefusal",
    "OpenAIClient",
    "get_llm_client",
]
