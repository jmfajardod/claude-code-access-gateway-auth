"""AgentCore Gateway REQUEST interceptor - scope-based access control.

Decodes the JWT from the Authorization header (no re-verification — AgentCore's
CUSTOM_JWT authorizer already validated the signature, issuer, audience, and
expiry).  Extracts the 'scope' claim and blocks tool invocations the user is
not authorised to execute.

Scope convention
----------------
  tool:get_weather   -> may call get_weather
  tool:get_time      -> may call get_time
  tool:*             -> may call any tool
  (no tool: scopes)  -> allow all tools (default for tokens with only OIDC scopes)

For tools/call, unauthorized requests return a JSON-RPC error response
(HTTP 200, short-circuits the Gateway so the backend Lambda is never invoked).
All other methods (initialize, tools/list, ping …) are passed through unchanged.
"""

import base64
import json
import logging
import typing

_LOGGER = logging.getLogger()
_LOGGER.setLevel(logging.INFO)

# Unprefixed tool name -> required scope.
# AgentCore namespaces tools per target as "<TargetName>___<tool>"
# (triple underscore); we strip the prefix before looking up scopes.
_TOOL_SCOPES: dict[str, str] = {
    "get_weather": "tool:get_weather",
    "get_time": "tool:get_time",
}

_TARGET_PREFIX_SEP = "___"


def _unprefix_tool(name: str) -> str:
    _, sep, tail = name.partition(_TARGET_PREFIX_SEP)
    return tail if sep else name


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


def _has_tool_scope(scopes: list[str], tool_name: str) -> bool:
    """Return True if scopes grant access to tool_name."""
    # No tool-specific scopes present → allow all (token uses only OIDC scopes)
    if not any(s.startswith("tool:") for s in scopes):
        return True
    if "tool:*" in scopes:
        return True
    required = _TOOL_SCOPES.get(_unprefix_tool(tool_name))
    # Unknown tool: let the gateway handle it
    if required is None:
        return True
    return required in scopes


def _jsonrpc_error(req_id, code: int, message: str) -> dict:
    """Short-circuit response: JSON-RPC error, HTTP 200, backend never called."""
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": code, "message": message},
                },
            }
        },
    }


def handler(event: dict, context: typing.Any) -> dict:
    _LOGGER.info("Request interceptor: %s", json.dumps(event))

    mcp = event.get("mcp", {})
    gateway_request = mcp.get("gatewayRequest", {})
    headers = gateway_request.get("headers", {}) or {}
    body = gateway_request.get("body", {}) or {}

    method = body.get("method", "")
    auth_header = _find_header(headers, "Authorization")

    # Enforce scope only for tools/call
    if method == "tools/call":
        tool_name = (body.get("params") or {}).get("name", "")
        req_id = body.get("id")

        if not auth_header.startswith("Bearer "):
            _LOGGER.warning("tools/call missing bearer token")
            return _jsonrpc_error(req_id, -32600, "Missing bearer token")

        token = auth_header[len("Bearer "):]
        claims = _decode_jwt_claims(token)
        scope_str = claims.get("scope", "") or ""
        scopes = [s for s in scope_str.split() if s]

        _LOGGER.info("tools/call tool=%s scopes=%s", tool_name, scopes)

        if not _has_tool_scope(scopes, tool_name):
            _LOGGER.warning("Unauthorized tool call: %s (scopes: %s)", tool_name, scopes)
            return _jsonrpc_error(
                req_id, -32600, f"Insufficient scope for tool: {tool_name}"
            )

    # Authorized (or non-tools/call method): pass through
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "headers": {
                    "Authorization": auth_header,
                    "Content-Type": "application/json",
                },
                "body": body,
            }
        },
    }

