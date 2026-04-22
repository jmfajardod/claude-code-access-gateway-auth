"""AgentCore Gateway RESPONSE interceptor - dynamic tool filtering.

Intercepted after the target Lambda responds, before the response reaches the
MCP client.  When the response is a tools/list result, filters the tool array
to include only tools the JWT-holder is authorised to use.

The original request Authorization header is available in
  event['mcp']['gatewayResponse']['headers']['Authorization']
when pass_request_headers=True is set on the Gateway interceptor config.

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

# tool name -> required scope
_TOOL_SCOPES: dict[str, str] = {
    "get_weather": "tool:get_weather",
    "get_time": "tool:get_time",
}


def _find_header(headers: dict, name: str) -> str:
    name_lower = name.lower()
    for k, v in headers.items():
        if k.lower() == name_lower:
            return v or ""
    return ""


def _decode_jwt_claims(token: str) -> dict:
    """Base64URL-decode the JWT payload without verifying the signature."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        padded = parts[1] + "==" * ((4 - len(parts[1]) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:
        _LOGGER.warning("JWT decode failed: %s", exc)
        return {}


def _is_tool_visible(tool_name: str, scopes: list[str]) -> bool:
    """Return True if the tool should be included in the filtered list."""
    if not any(s.startswith("tool:") for s in scopes):
        return True  # no tool scopes -> show all
    if "tool:*" in scopes:
        return True
    required = _TOOL_SCOPES.get(tool_name)
    return required is None or required in scopes


def handler(event: dict, context: typing.Any) -> dict:
    _LOGGER.info("Response interceptor: %s", json.dumps(event))

    mcp = event.get("mcp", {})
    gateway_response = mcp.get("gatewayResponse", {}) or {}
    status_code = gateway_response.get("statusCode", 200)
    response_body = gateway_response.get("body", {})

    # Extract scopes from JWT in the original request Authorization header.
    # AgentCore injects this into gatewayResponse.headers when pass_request_headers=True.
    resp_headers = gateway_response.get("headers", {}) or {}
    auth_header = _find_header(resp_headers, "Authorization")
    scopes: list[str] = []

    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):]
        claims = _decode_jwt_claims(token)
        scope_str = claims.get("scope", "") or ""
        scopes = [s for s in scope_str.split() if s]

    # Filter tools/list result
    if (
        isinstance(response_body, dict)
        and isinstance(response_body.get("result"), dict)
        and "tools" in response_body["result"]
    ):
        all_tools = response_body["result"]["tools"] or []
        filtered = [t for t in all_tools if _is_tool_visible(t.get("name", ""), scopes)]
        _LOGGER.info(
            "Tool filter: %d -> %d visible (scopes: %s)", len(all_tools), len(filtered), scopes
        )
        response_body = {
            **response_body,
            "result": {**response_body["result"], "tools": filtered},
        }

    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": status_code,
                "body": response_body,
            }
        },
    }
