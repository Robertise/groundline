import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.llm.base import LLMResult, LLMError


class BedrockLLM:
    def __init__(self, model_id: str, region: str, client=None):
        self.model_id = model_id
        self.region = region
        self.client = client or boto3.client("bedrock-runtime", region_name=region)

    def call_tool(
        self,
        system: str,
        user: str,
        tool_name: str,
        tool_schema: dict,
        max_tokens: int = 2000,
    ) -> LLMResult:
        messages = [{"role": "user", "content": [{"text": user}]}]
        tool_config = {
            "tools": [
                {
                    "toolSpec": {
                        "name": tool_name,
                        "description": "Return the result.",
                        "inputSchema": {"json": tool_schema},
                    }
                }
            ],
            "toolChoice": {"tool": {"name": tool_name}},
        }
        inference_config = {"maxTokens": max_tokens, "temperature": 0}

        try:
            resp = self.client.converse(
                modelId=self.model_id,
                system=[{"text": system}],
                messages=messages,
                inferenceConfig=inference_config,
                toolConfig=tool_config,
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "ClientError")
            msg = e.response.get("Error", {}).get("Message", str(e))
            raise LLMError(f"{code}: {msg}") from e
        except BotoCoreError as e:
            raise LLMError(f"BotoCoreError: {e}") from e
        except Exception as e:
            raise LLMError(f"Bedrock invocation failed: {e}") from e

        output = resp.get("output", {})
        message = output.get("message", {})
        content = message.get("content", [])

        tool_input = None
        for block in content:
            if "toolUse" in block and block["toolUse"].get("name") == tool_name:
                tool_input = block["toolUse"].get("input")
                break

        if tool_input is None:
            raise LLMError(f"No toolUse block for {tool_name} returned in output")

        usage = resp.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)

        return LLMResult(
            data=tool_input,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
