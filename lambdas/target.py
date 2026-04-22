"""Dummy MCP target Lambda - exposes get_weather and get_time tools."""

import json
import logging
import typing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger()
_LOGGER.setLevel(logging.INFO)


def handler(event: dict, context: typing.Any) -> dict:
    _LOGGER.info("Target event: %s", json.dumps(event))

    mcp = event.get("mcp", {})
    body = mcp.get("request", {}).get("body", {})
    method = body.get("method", "")

    if method == "initialize":
        return _mcp_result(
            body.get("id"),
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mcp-auth-spike", "version": "0.1.0"},
            },
        )

    if method == "tools/list":
        return _mcp_result(
            body.get("id"),
            {
                "tools": [
                    {
                        "name": "get_weather",
                        "description": "Get weather for a location",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"location": {"type": "string"}},
                            "required": ["location"],
                        },
                    },
                    {
                        "name": "get_time",
                        "description": "Get current time for a timezone",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"timezone": {"type": "string"}},
                            "required": ["timezone"],
                        },
                    },
                ]
            },
        )

    if method == "tools/call":
        tool_name = body.get("params", {}).get("name", "")
        arguments = body.get("params", {}).get("arguments", {})

        if tool_name == "get_weather":
            location = arguments.get("location", "unknown")
            return _mcp_result(
                body.get("id"),
                {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Weather in {location}: 72F, sunny",
                        }
                    ]
                },
            )

        if tool_name == "get_time":
            tz_name = arguments.get("timezone", "UTC")
            try:
                now = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
            except Exception:
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            return _mcp_result(
                body.get("id"),
                {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Current time in {tz_name}: {now}",
                        }
                    ]
                },
            )

    return _mcp_error(body.get("id"), -32601, f"Method not found: {method}")


def _mcp_result(req_id: typing.Any, result: dict) -> dict:
    return {
        "mcp": {
            "response": {
                "body": {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": result,
                }
            }
        }
    }


def _mcp_error(req_id: typing.Any, code: int, message: str) -> dict:
    return {
        "mcp": {
            "response": {
                "body": {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": code,
                        "message": message,
                    },
                }
            }
        }
    }
