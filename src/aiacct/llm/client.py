"""OpenAI access.

The official SDK rather than a framework wrapper, because this system needs
``input_file``, ``input_image``, PDF ``detail``, and strict structured outputs
on a very recent model, and the SDK exposes those directly. LangGraph still
owns orchestration; this module only makes calls.

Every call goes through :meth:`OpenAIClient.parse`, which always returns a
parsed Pydantic object or raises. Nothing downstream sees raw JSON.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Images are not valid input_file content; they must go through input_image.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class LLMError(RuntimeError):
    pass


class ModelRefusal(LLMError):
    """The model declined to answer. Distinct from a transport failure."""


@dataclass
class LLMResult(Generic[T]):
    parsed: T
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 1


@dataclass
class Usage:
    """Running total across a process, for the per-run cost figure."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict[str, int] = field(default_factory=dict)

    def record(self, result: LLMResult) -> None:
        self.calls += 1
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.by_model[result.model] = self.by_model.get(result.model, 0) + 1


class LLMClient(Protocol):
    """What the pipeline depends on. The stub implements the same surface.

    ``source_hint`` names the file a call is about even when its content is
    being passed as text rather than uploaded. The real client ignores it; the
    offline provider uses it to find the transcript it replays.
    """

    def parse(
        self,
        *,
        prompt: str,
        schema: type[T],
        files: list[Path] | None = ...,
        images: list[Path] | None = ...,
        effort: str = ...,
        detail: str = ...,
        source_hint: Path | None = ...,
    ) -> LLMResult[T]: ...


class OpenAIClient:
    def __init__(self, settings: Settings | None = None) -> None:
        from openai import OpenAI

        self.settings = settings or get_settings()
        if not self.settings.openai_api_key:
            raise LLMError(
                "OPENAI_API_KEY is not set. Set it in .env, or set use_stub_llm=true "
                "to run against the deterministic offline provider."
            )
        self._client = OpenAI(
            api_key=self.settings.openai_api_key,
            timeout=self.settings.llm_timeout_seconds,
        )
        self.usage = Usage()
        self._file_cache: dict[str, str] = {}
        # The Files API purpose for model-readable documents. Sources disagree
        # between "user_data" and "assistants"; the first failure switches.
        self._file_purpose = "user_data"

    # -------------------------------------------------------------- uploads

    def upload(self, path: Path) -> str:
        """Upload a file and return its id, caching by absolute path."""
        key = str(path.resolve())
        if key in self._file_cache:
            return self._file_cache[key]

        for purpose in (self._file_purpose, "assistants"):
            try:
                with open(path, "rb") as handle:
                    uploaded = self._client.files.create(file=handle, purpose=purpose)
                self._file_purpose = purpose
                self._file_cache[key] = uploaded.id
                return uploaded.id
            except Exception as exc:  # noqa: BLE001 - narrow via message below
                if "purpose" not in str(exc).lower():
                    raise
                log.warning("Files API rejected purpose=%s, retrying: %s", purpose, exc)
        raise LLMError(f"could not upload {path.name}: no accepted purpose value")

    @staticmethod
    def _image_part(path: Path) -> dict[str, Any]:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"}

    # -------------------------------------------------------------- calling

    def parse(
        self,
        *,
        prompt: str,
        schema: type[T],
        files: list[Path] | None = None,
        images: list[Path] | None = None,
        effort: str = "medium",
        detail: str = "high",
        source_hint: Path | None = None,  # noqa: ARG002 - for the offline provider
    ) -> LLMResult[T]:
        content: list[dict[str, Any]] = []

        for path in files or []:
            if path.suffix.lower() in IMAGE_SUFFIXES:
                # Not a valid input_file type - route it correctly rather than
                # letting the API reject it.
                content.append(self._image_part(path))
            else:
                part: dict[str, Any] = {"type": "input_file", "file_id": self.upload(path)}
                if path.suffix.lower() == ".pdf":
                    part["detail"] = detail
                content.append(part)

        for path in images or []:
            content.append(self._image_part(path))

        content.append({"type": "input_text", "text": prompt})
        messages = [{"role": "user", "content": content}]

        models = [self.settings.model_primary, *self.settings.model_fallbacks]
        last_error: Exception | None = None
        attempts = 0

        for model in models:
            for retry in range(self.settings.llm_max_retries):
                attempts += 1
                try:
                    response = self._client.responses.parse(
                        model=model,
                        input=messages,
                        text_format=schema,
                        reasoning={"effort": effort},
                    )
                    parsed = self._extract(response, schema)
                    usage = getattr(response, "usage", None)
                    result = LLMResult(
                        parsed=parsed,
                        model=model,
                        input_tokens=getattr(usage, "input_tokens", 0) or 0,
                        output_tokens=getattr(usage, "output_tokens", 0) or 0,
                        attempts=attempts,
                    )
                    self.usage.record(result)
                    return result
                except ModelRefusal:
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    wait = 2**retry
                    log.warning(
                        "model=%s attempt=%d failed (%s); retrying in %ss",
                        model, retry + 1, exc, wait,
                    )
                    time.sleep(wait)
            log.warning("model=%s exhausted retries, falling back", model)

        raise LLMError(f"all models failed after {attempts} attempts: {last_error}")

    @staticmethod
    def _extract(response: Any, schema: type[T]) -> T:
        if getattr(response, "status", None) == "incomplete":
            reason = getattr(getattr(response, "incomplete_details", None), "reason", "unknown")
            raise LLMError(f"response incomplete: {reason}")

        for item in getattr(response, "output", []) or []:
            for part in getattr(item, "content", []) or []:
                if getattr(part, "type", None) == "refusal":
                    raise ModelRefusal(getattr(part, "refusal", "refused"))

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise LLMError("no parsed output returned")
        return parsed


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """Return the configured provider.

    The stub keeps the pipeline, the tests, and the synthetic data steps
    runnable with no key and no network.
    """
    settings = settings or get_settings()
    if settings.use_stub_llm or not settings.openai_api_key:
        from .stub import StubClient

        return StubClient(settings)
    return OpenAIClient(settings)
