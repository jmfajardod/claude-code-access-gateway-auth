"""MCP target Lambda — invoked by AgentCore Gateway for tools/call.

AgentCore does all MCP/JSON-RPC framing itself.  Contract here:

  event                                                    -> raw tool arguments dict
  context.client_context.custom["bedrockAgentCoreToolName"]-> "<TargetName>___<tool>"
                                                              (triple underscore)

Return value -> HTTP-proxy shape: {"statusCode": 200, "body": <json-string>}.
AgentCore unwraps `body` and emits it back to the MCP client inside a
tools/call `result.content[].text` envelope.

`initialize` and `tools/list` are NOT routed here — AgentCore answers those
from the inline tool schema registered on the CfnGatewayTarget.
"""

import json
import logging
import typing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger()
_LOGGER.setLevel(logging.INFO)

_TARGET_PREFIX_SEP = "___"


def _unprefix_tool(name: str) -> str:
    _, sep, tail = name.partition(_TARGET_PREFIX_SEP)
    return tail if sep else name


def _ok(payload: dict) -> dict:
    return {"statusCode": 200, "body": json.dumps(payload)}


def _error(code: int, message: str) -> dict:
    return {"statusCode": code, "body": json.dumps({"error": message})}


def handler(event: dict, context: typing.Any) -> dict:
    tool_name = ""
    try:
        if getattr(context, "client_context", None) is not None:
            custom = context.client_context.custom or {}
            tool_name = custom.get("bedrockAgentCoreToolName", "")
    except Exception as exc:
        _LOGGER.warning("Failed to read client_context: %s", exc)

    _LOGGER.info("Target invoked: tool=%r args=%s", tool_name, json.dumps(event))

    unprefixed = _unprefix_tool(tool_name)

    if unprefixed == "get_weather":
        location = event.get("location", "unknown")
        return _ok({"weather": f"Weather in {location}: 72F, sunny"})

    if unprefixed == "get_time":
        tz_name = event.get("timezone", "UTC")
        try:
            now = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return _ok({"time": f"Current time in {tz_name}: {now}"})

    return _error(400, f"Unknown tool: {tool_name!r}")
