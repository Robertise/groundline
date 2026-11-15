import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.llm.base import LLMResult, LLMError, LLMOutputError


def make_bedrock_client(
    region: str,
    access_key_id: str = "",
    secret_access_key: str = "",
    session_token: str = "",
    profile: str = "",
):
    """Explicit keys from settings win; else a named profile; else the default AWS chain."""
    if access_key_id and secret_access_key:
        session = boto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            aws_session_token=session_token or None,
            region_name=region,
        )
    elif access_key_id or secret_access_key:
        raise LLMError(
            "Set both GROUNDLINE_AWS_ACCESS_KEY_ID and GROUNDLINE_AWS_SECRET_ACCESS_KEY, or neither"
        )
    elif profile:
        session = boto3.Session(profile_name=profile, region_name=region)
    else:
        session = boto3.Session(region_name=region)
    return session.client("bedrock-runtime")


class BedrockLLM:
    def __init__(
        self,
        model_id: str,
        region: str,
        client=None,
        access_key_id: str = "",
        secret_access_key: str = "",
        session_token: str = "",
        profile: str = "",
    ):
        self.model_id = model_id
        self.region = region
        self.client = (
            client
            if client is not None
            else make_bedrock_client(
                region=region,
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
                session_token=session_token,
                profile=profile,
            )
        )

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

        usage = resp.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)

        if resp.get("stopReason") == "max_tokens":
            raise LLMOutputError(
                "truncated: the model reply reached max_tokens",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        tool_input = None
        for block in content:
            if "toolUse" in block and block["toolUse"].get("name") == tool_name:
                tool_input = block["toolUse"].get("input")
                break

        if tool_input is None:
            raise LLMError(f"No toolUse block for {tool_name} returned in output")

        return LLMResult(
            data=tool_input,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
