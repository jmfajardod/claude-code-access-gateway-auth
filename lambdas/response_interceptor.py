"""AgentCore Gateway RESPONSE interceptor - dynamic tool filtering.

Intercepted after the target Lambda responds, before the response reaches the
MCP client.  When the response is a tools/list result, filters the tool array
to include only tools the JWT-holder is authorised to use.

Event shape (AgentCore RESPONSE interceptor, with pass_request_headers=True)
----------------------------------------------------------------------------
  event["mcp"]["gatewayRequest"]["headers"]["authorization"]   <- JWT lives here
  event["mcp"]["gatewayRequest"]["body"]["method"]             <- e.g. "tools/list"
  event["mcp"]["gatewayResponse"]["statusCode"]                <- upstream status
  event["mcp"]["gatewayResponse"]["body"]                      <- JSON-RPC result
                                                                  (may be null for
                                                                  MCP notifications)

Tool naming
-----------
AgentCore namespaces tools as "<TargetName>___<tool>" (triple underscore), so
tools/list returns e.g. "DummyToolsTarget___get_weather".  We strip that prefix
before looking up scopes.

Scope convention
----------------
  tool:get_weather   -> tool visible
  tool:get_time      -> tool visible
  tool:*             -> all tools visible
  (no tool: scopes)  -> all tools visible (default)
"""

import base64
import json
import logging
import typing

_LOGGER = logging.getLogger()
_LOGGER.setLevel(logging.INFO)

# Unprefixed tool name -> required scope
_TOOL_SCOPES: dict[str, str] = {
    "get_weather": "tool:get_weather",
    "get_time": "tool:get_time",
}

_TARGET_PREFIX_SEP = "___"


def _find_header(headers: dict, name: str) -> str:
    name_lower = name.lower()
    for k, v in headers.items():
        if k.lower() == name_lower:
            return v or ""
    return ""


def _decode_jwt_claims(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        padded = parts[1] + "==" * ((4 - len(parts[1]) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:
        _LOGGER.warning("JWT decode failed: %s", exc)
        return {}


def _unprefix_tool(name: str) -> str:
    """Strip the "<TargetName>___" prefix AgentCore adds to tool names."""
    _, sep, tail = name.partition(_TARGET_PREFIX_SEP)
    return tail if sep else name


def _is_tool_visible(tool_name: str, scopes: list[str]) -> bool:
    if not any(s.startswith("tool:") for s in scopes):
        return True  # no tool scopes -> show all
    if "tool:*" in scopes:
        return True
    required = _TOOL_SCOPES.get(_unprefix_tool(tool_name))
    return required is None or required in scopes


def _passthrough(status_code: int, body: typing.Any) -> dict:
    out: dict = {"statusCode": status_code}
    if body is not None:
        out["body"] = body
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayResponse": out},
    }


def handler(event: dict, context: typing.Any) -> dict:
    _LOGGER.info("Response interceptor: %s", json.dumps(event))

    mcp = event.get("mcp", {}) or {}
    gateway_request = mcp.get("gatewayRequest", {}) or {}
    gateway_response = mcp.get("gatewayResponse", {}) or {}

    status_code = gateway_response.get("statusCode", 200)
    response_body = gateway_response.get("body")

    # Only touch tools/list responses with a JSON-RPC result.body.
    req_body = gateway_request.get("body", {}) or {}
    method = req_body.get("method", "")

    if method != "tools/list" or not isinstance(response_body, dict):
        return _passthrough(status_code, response_body)

    result = response_body.get("result")
    if not isinstance(result, dict) or "tools" not in result:
        return _passthrough(status_code, response_body)

    # Read the JWT from the *request* headers (pass_request_headers=True).
    req_headers = gateway_request.get("headers", {}) or {}
    auth_header = _find_header(req_headers, "Authorization")
    scopes: list[str] = []
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):]
        claims = _decode_jwt_claims(token)
        scope_str = claims.get("scope", "") or ""
        scopes = [s for s in scope_str.split() if s]

    all_tools = result.get("tools") or []
    filtered = [t for t in all_tools if _is_tool_visible(t.get("name", ""), scopes)]
    _LOGGER.info(
        "Tool filter: %d -> %d visible (scopes=%s, names=%s)",
        len(all_tools),
        len(filtered),
        scopes,
        [t.get("name") for t in filtered],
    )

    new_body = {
        **response_body,
        "result": {**result, "tools": filtered},
    }
    return _passthrough(status_code, new_body)
