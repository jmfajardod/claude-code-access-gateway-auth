"""AgentCore Gateway RESPONSE interceptor.

Two responsibilities:

1. **tools/list filtering** — drops tools the JWT-holder has no scope for so
   unauthorised tools never appear in the client's picker.

2. **tools/call content transform (experimental)** — for `query_data`, appends
   a `resource_link` MCP content block alongside the existing text block,
   exposing the presigned S3 URL as a first-class MCP resource. This probes
   whether AgentCore accepts non-text content blocks in transformed responses,
   and whether MCP clients (Inspector, Claude.ai, Claude Desktop) do anything
   more useful with a `resource_link` than with the URL inside a text block.
   If the transform is rejected or clients ignore it, reverting is a no-op —
   the original text block is unchanged.

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
  tool:query_data    -> tool visible
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
    "query_data": "tool:query_data",
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


def _append_query_data_resource_link(body: dict) -> dict:
    """Append a `resource_link` content block when the target is query_data.

    Leaves the existing text content block unchanged; adds a second block that
    references `full_results.presigned_url` with MCP-native metadata.  Returns
    `body` unchanged if the expected envelope shape isn't present.
    """
    result = body.get("result")
    if not isinstance(result, dict):
        return body
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return body
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        return body
    try:
        parsed = json.loads(first.get("text") or "")
    except (json.JSONDecodeError, TypeError):
        return body
    # AgentCore passes the target Lambda's raw HTTP-proxy return through to
    # the interceptor, so content[0].text is {"statusCode":200,"body":"<env>"}.
    # Unwrap one level to reach the actual envelope.
    envelope = parsed
    if isinstance(parsed, dict) and "body" in parsed and "statusCode" in parsed:
        try:
            envelope = json.loads(parsed["body"])
        except (json.JSONDecodeError, TypeError):
            return body
    if not isinstance(envelope, dict):
        return body
    full_results = envelope.get("full_results") or {}
    presigned_url = full_results.get("presigned_url")
    if not presigned_url:
        return body

    size_bytes = full_results.get("size_bytes")
    row_count = envelope.get("row_count")
    descr_parts: list[str] = []
    if row_count is not None:
        descr_parts.append(f"{row_count:,} rows")
    if isinstance(size_bytes, int):
        descr_parts.append(f"~{max(1, round(size_bytes / 1024)):,} KB")
    description = (
        "Full dataset (" + ", ".join(descr_parts) + ")"
        if descr_parts
        else "Full dataset"
    )
    mime_type = "text/csv" if full_results.get("format") == "csv" else "application/octet-stream"

    resource_link = {
        "type": "resource_link",
        "uri": presigned_url,
        "name": "sample_sales.csv",
        "description": description,
        "mimeType": mime_type,
    }

    _LOGGER.info(
        "query_data resource_link appended: uri_ttl=%ss size=%s rows=%s",
        full_results.get("presigned_url_ttl_seconds"),
        size_bytes,
        row_count,
    )
    return {
        **body,
        "result": {**result, "content": [*content, resource_link]},
    }


def handler(event: dict, context: typing.Any) -> dict:
    _LOGGER.info("Response interceptor: %s", json.dumps(event))

    mcp = event.get("mcp", {}) or {}
    gateway_request = mcp.get("gatewayRequest", {}) or {}
    gateway_response = mcp.get("gatewayResponse", {}) or {}

    status_code = gateway_response.get("statusCode", 200)
    response_body = gateway_response.get("body")

    req_body = gateway_request.get("body", {}) or {}
    method = req_body.get("method", "")

    if not isinstance(response_body, dict):
        return _passthrough(status_code, response_body)

    # --- tools/list: filter visible tools by JWT scope -------------------------
    if method == "tools/list":
        result = response_body.get("result")
        if not isinstance(result, dict) or "tools" not in result:
            return _passthrough(status_code, response_body)

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

    # --- tools/call: append resource_link for query_data only ------------------
    if method == "tools/call":
        tool_name = (req_body.get("params") or {}).get("name", "")
        if _unprefix_tool(tool_name) == "query_data":
            transformed = _append_query_data_resource_link(response_body)
            return _passthrough(status_code, transformed)

    # Everything else: pass through untouched
    return _passthrough(status_code, response_body)
