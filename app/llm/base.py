from dataclasses import dataclass
import json
from typing import Protocol


@dataclass(frozen=True)
class LLMResult:
    data: dict
    input_tokens: int
    output_tokens: int


class LLMError(Exception):
    pass


class LLMOutputError(LLMError):
    def __init__(
        self,
        message: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        raw: str = "",
    ):
        super().__init__(message)
        self.message = message
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.raw = raw


def coerce_json_fields(
    data: dict,
    list_fields: tuple[str, ...] = (),
    dict_fields: tuple[str, ...] = (),
) -> dict:
    """If a listed field is a string, try json.loads; keep it only if the result has the expected type."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for f in list_fields:
        if f in out and isinstance(out[f], str):
            try:
                parsed = json.loads(out[f])
                if isinstance(parsed, list):
                    out[f] = parsed
            except Exception:
                pass
    for f in dict_fields:
        if f in out and isinstance(out[f], str):
            try:
                parsed = json.loads(out[f])
                if isinstance(parsed, dict):
                    out[f] = parsed
            except Exception:
                pass
    return out


class LLM(Protocol):
    def call_tool(
        self,
        system: str,
        user: str,
        tool_name: str,
        tool_schema: dict,
        max_tokens: int = 2000,
    ) -> LLMResult: ...
