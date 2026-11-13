from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LLMResult:
    data: dict
    input_tokens: int
    output_tokens: int


class LLMError(Exception):
    pass


class LLM(Protocol):
    def call_tool(
        self,
        system: str,
        user: str,
        tool_name: str,
        tool_schema: dict,
        max_tokens: int = 2000,
    ) -> LLMResult: ...
