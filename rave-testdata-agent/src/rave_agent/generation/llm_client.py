"""Anthropic API wrapper (FR-6.1, FR-6.8).

The only component that talks to the LLM. Responses are constrained by a JSON
schema so the reply is structurally valid; semantic validation is the
validator's job, not this module's.

Secrets never appear in prompts or logs - the API key goes to the SDK only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..utils.logging import get_logger

log = get_logger(__name__)


class LlmError(RuntimeError):
    """The model could not be reached, or returned something unusable."""


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass
class LlmResponse:
    data: dict
    input_tokens: int
    output_tokens: int
    raw_text: str = ""


@dataclass
class LlmClient:
    """Note: `temperature` is deliberately not sent.

    Current Claude models (Sonnet 5 and the 4.6+ family) removed the sampling
    parameters; passing `temperature` returns
    `400 invalid_request_error: temperature is deprecated for this model`.
    The config key is kept for older models but ignored here, and variety in
    generated data comes from the prompt instead.
    """

    api_key: str
    model: str
    max_tokens: int = 16000
    usage: TokenUsage = field(default_factory=TokenUsage)

    def __post_init__(self) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - environment problem
            raise LlmError("the `anthropic` package is not installed") from exc
        self._client = anthropic.Anthropic(api_key=self.api_key)
        self._anthropic = anthropic

    # ------------------------------------------------------------------
    def generate(
        self,
        system: str,
        messages: list[dict],
        schema: dict,
        label: str = "",
    ) -> LlmResponse:
        """One schema-constrained call, with a plain-JSON fallback.

        Wide CRF forms can be refused by the schema compiler - either
        `Schema is too complex` or `Grammar compilation timed out`. Rather than
        drop those forms, the call is retried asking for plain JSON and the
        response is parsed defensively (FR-6.3). Correctness does not depend on the
        schema either way - the deterministic validator is the guarantee
        (FR-6.4); the schema only reduces repair round trips.
        """
        try:
            response = self._call(system, messages, schema)
        except self._anthropic.APIStatusError as exc:
            if not _is_schema_rejection(exc):
                raise LlmError(
                    f"{type(exc).__name__} {exc.status_code}: {str(exc)[:300]}") from exc
            log.info("schema rejected; retrying as plain JSON",
                     extra={"label": label, "reason": str(exc)[:120]})
            try:
                response = self._call(system, _with_json_instruction(messages, schema), None)
            except self._anthropic.APIError as inner:
                raise LlmError(
                    f"{type(inner).__name__}: {str(inner)[:300]}") from inner
        except self._anthropic.APIError as exc:
            raise LlmError(f"{type(exc).__name__}: {str(exc)[:300]}") from exc
        except Exception as exc:  # noqa: BLE001
            # Transport failures surface as httpx errors, not SDK ones - e.g.
            # RemoteProtocolError on a dropped stream. Wrap them so one bad
            # response fails a single form instead of aborting the whole run.
            raise LlmError(f"{type(exc).__name__}: {str(exc)[:300]}") from exc

        if getattr(response, "stop_reason", None) == "refusal":
            raise LlmError(
                "the model declined to generate this form "
                f"({getattr(response, 'stop_details', None)})"
            )

        text = "".join(
            block.text for block in response.content
            if getattr(block, "type", "") == "text"
        ).strip()

        self.usage.add(response.usage.input_tokens, response.usage.output_tokens)
        log.info("llm call", extra={
            "label": label, "model": self.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        })

        # Parse defensively: the schema path guarantees clean JSON, the fallback
        # path does not (FR-6.3).
        try:
            data = json.loads(_strip_fences(text))
        except json.JSONDecodeError as exc:
            if getattr(response, "stop_reason", None) == "max_tokens":
                raise LlmError(
                    f"response was cut off at max_tokens ({self.max_tokens}); the form "
                    "is too large to answer in one reply. Raise generation.max_tokens "
                    "or split the form."
                ) from exc
            raise LlmError(
                f"response was not valid JSON: {exc}. "
                f"First 200 chars: {text[:200]!r}"
            ) from exc

        if not isinstance(data, dict):
            raise LlmError(f"expected a JSON object, got {type(data).__name__}")

        return LlmResponse(
            data=data,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            raw_text=text,
        )

    def _call(self, system: str, messages: list[dict], schema: dict | None):
        """Always stream.

        A wide CRF form with every field populated needs a large `max_tokens`,
        and the SDK refuses a non-streaming request that could run past its
        timeout:

            ValueError: Streaming is required for operations that may take
            longer than 10 minutes.

        Streaming also avoids an HTTP timeout on the long replies those forms
        produce. `get_final_message()` returns the same Message object the
        non-streaming call would have.
        """
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if schema is not None:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        with self._client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()


# Ways the API can refuse a generated JSON schema. All are recoverable by
# dropping the constraint and asking for plain JSON instead - the deterministic
# validator is what actually guarantees correctness (FR-6.4).
_SCHEMA_REJECTIONS = (
    "too complex",
    "grammar compilation",
    "output_config.format.schema",
)


def _is_schema_rejection(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _SCHEMA_REJECTIONS)


def _with_json_instruction(messages: list[dict], schema: dict) -> list[dict]:
    """Restate the schema in the prompt when it cannot be enforced natively."""
    instruction = (
        "Return ONLY a single JSON object, with no prose and no markdown fences. "
        "It must conform to this JSON Schema:\n"
        + json.dumps(schema, separators=(",", ":"))
    )
    out = list(messages)
    if out and out[-1]["role"] == "user":
        out[-1] = {"role": "user", "content": out[-1]["content"] + "\n\n" + instruction}
    else:
        out.append({"role": "user", "content": instruction})
    return out


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    """Tolerate markdown fences the fallback path may produce."""
    cleaned = _FENCE.sub("", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return cleaned[start:end + 1]
    return cleaned
