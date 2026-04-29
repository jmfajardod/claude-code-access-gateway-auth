"""FastMCP server for the McpLambdaSpikeStack — runs on Lambda via Lambda Web Adapter.

This replaces the AgentCore Gateway path of the legacy McpAuthSpikeStack with a
plain Python MCP server that:

- Hosts the MCP streamable-HTTP endpoint at `/mcp` (stateless: each request is
  independent, no in-memory session).
- Validates Stytch Connected Apps JWTs via FastMCP's JWTVerifier (Stytch JWKS).
- Auto-hosts RFC 9728 Protected Resource Metadata at
  `/.well-known/oauth-protected-resource/mcp` so clients can discover Stytch as
  the authorization server (RemoteAuthProvider does this for us).
- Enforces scope-based RBAC inside each tool function (DEFAULT-DENY: a token
  without `tool:*` or the matching `tool:<name>` scope cannot call the tool).
- Filters `tools/list` via a FastMCP middleware so unauthorized tools never
  appear in the picker.
- For `query_data`, returns BOTH a TextContent envelope AND a ResourceLink
  block pointing at a presigned S3 URL.

Environment
-----------
STYTCH_PROJECT_DOMAIN     https://<slug>.customers.stytch.dev   (no trailing slash)
MCP_SERVER_URL            https://<id>.execute-api.<region>.amazonaws.com/mcp
RESULTS_BUCKET            S3 bucket holding sample_sales.csv
RESULTS_OBJECT_KEY        e.g. sample_sales.csv
PRESIGNED_URL_TTL_SECONDS Default 300

Deployment
----------
Packaged as a container image with AWS Lambda Web Adapter; uvicorn serves the
ASGI app on port 8080 and LWA proxies API Gateway events to it.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

import boto3
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.types import (
    CallToolRequestParams,
    ListToolsRequest,
    ResourceLink,
    TextContent,
    Tool,
)

_LOGGER = logging.getLogger("mcp-lambda-spike")
_LOGGER.setLevel(logging.INFO)

# -- Environment ---------------------------------------------------------------
_STYTCH_DOMAIN = os.environ["STYTCH_PROJECT_DOMAIN"].rstrip("/")
_MCP_SERVER_URL = os.environ["MCP_SERVER_URL"].rstrip("/")
_RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "")
_RESULTS_OBJECT_KEY = os.environ.get("RESULTS_OBJECT_KEY", "")
_PRESIGNED_URL_TTL_SECONDS = int(os.environ.get("PRESIGNED_URL_TTL_SECONDS", "300"))

# `MCP_SERVER_URL` is the full MCP endpoint (`<api-gw-base>/mcp`) — the JWT `aud`
# value clients pass via RFC 8707 `resource=<MCP_SERVER_URL>`.  The API Gateway
# base (no `/mcp`) is what FastMCP wants as `base_url`; it appends the mount
# path itself when computing the protected-resource metadata URL.
_MCP_PATH = "/mcp"
_API_GW_BASE_URL = (
    _MCP_SERVER_URL[: -len(_MCP_PATH)] if _MCP_SERVER_URL.endswith(_MCP_PATH) else _MCP_SERVER_URL
)

# -- Tool ↔ scope contract -----------------------------------------------------
# Default-DENY: every tool needs an explicit scope (or the wildcard).  This is
# the change vs. the legacy AgentCore stack, which fell back to default-open
# when a JWT had no `tool:*` scopes.
_TOOL_SCOPES: dict[str, str] = {
    "get_weather": "tool:get_weather",
    "get_time": "tool:get_time",
    "query_data": "tool:query_data",
}
_ADVERTISED_SCOPES = [
    "openid",
    "email",
    "profile",
    *_TOOL_SCOPES.values(),
    "tool:*",
]


def _scopes_from_token() -> list[str]:
    """Return the JWT's `scope` claim split into a list of scope strings.

    FastMCP's `AccessToken.scopes` is already a list[str], so no .split() needed.
    Returns [] if no token is bound to the request (shouldn't happen because the
    JWTVerifier rejects unauthenticated requests before they reach a tool).
    """
    try:
        token = get_access_token()
    except Exception:  # pragma: no cover - JWTVerifier should reject before this
        return []
    return list(token.scopes or [])


def _ensure_scope(tool: str) -> None:
    """Default-deny scope check.  Raise ToolError if the JWT lacks the scope."""
    required = _TOOL_SCOPES[tool]
    scopes = _scopes_from_token()
    if "tool:*" in scopes or required in scopes:
        return
    _LOGGER.info("Denied tool=%s scopes=%s required=%s", tool, scopes, required)
    raise ToolError(f"Insufficient scope for tool: {tool}")


# -- Auth provider -------------------------------------------------------------
_token_verifier = JWTVerifier(
    jwks_uri=f"{_STYTCH_DOMAIN}/.well-known/jwks.json",
    issuer=_STYTCH_DOMAIN,
    # Accept tokens whose `aud` is either the MCP server URL (clients sending
    # RFC 8707 `resource=<MCP_SERVER_URL>`) or the Stytch project domain
    # (Stytch's default audience).  Mirrors the AgentCore stack's policy.
    audience=[_MCP_SERVER_URL, _STYTCH_DOMAIN],
)

_auth_provider = RemoteAuthProvider(
    token_verifier=_token_verifier,
    authorization_servers=[_STYTCH_DOMAIN],
    base_url=_API_GW_BASE_URL,
    scopes_supported=_ADVERTISED_SCOPES,
    resource_name="MCP Lambda Spike",
)

mcp = FastMCP(
    "mcp-lambda-spike",
    instructions=(
        "MCP server for the McpLambdaSpikeStack auth spike. Tools: get_weather, "
        "get_time, query_data. Scope-based RBAC enforced server-side."
    ),
    auth=_auth_provider,
)


# -- Tools ---------------------------------------------------------------------

@mcp.tool()
def get_weather(location: str) -> str:
    """Get a (canned) weather report for a location."""
    _ensure_scope("get_weather")
    return f"Weather in {location}: 72F, sunny"


@mcp.tool()
def get_time(timezone_name: str) -> str:
    """Get the current time for an IANA timezone (e.g. America/New_York)."""
    _ensure_scope("get_time")
    try:
        now = datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"Current time in UTC (fallback from invalid zone {timezone_name!r}): {now}"
    return f"Current time in {timezone_name}: {now}"


# Module-level S3 client so it's reused across invocations.
_s3_client = boto3.client("s3")
_SAMPLE_ROW_COUNT = 50
_TOP_VALUES_MAX = 5
_LOW_CARDINALITY_THRESHOLD = 30
_TOOL_VERSION = "1.0.0-mcp-lambda-spike"


def _read_seed_csv() -> tuple[list[str], list[list[str]], int]:
    obj = _s3_client.get_object(Bucket=_RESULTS_BUCKET, Key=_RESULTS_OBJECT_KEY)
    size_bytes = int(obj.get("ContentLength", 0))
    text = obj["Body"].read().decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    rows = list(reader)
    return header, rows, size_bytes


def _infer_column_type(values: list[str]) -> str:
    sample = [v for v in values if v]
    if not sample:
        return "string"
    try:
        for v in sample:
            int(v)
        return "integer"
    except ValueError:
        pass
    try:
        for v in sample:
            float(v)
        return "number"
    except ValueError:
        pass
    try:
        for v in sample:
            datetime.fromisoformat(v)
        return "date"
    except ValueError:
        pass
    return "string"


def _compute_summary_stats(header: list[str], rows: list[list[str]]) -> dict:
    stats: dict = {}
    for idx, col_name in enumerate(header):
        col_values = [row[idx] if idx < len(row) else "" for row in rows]
        non_null = [v for v in col_values if v != ""]
        nulls = len(col_values) - len(non_null)
        col_type = _infer_column_type(non_null)
        if col_type in ("integer", "number") and non_null:
            nums = [float(v) for v in non_null]
            stats[col_name] = {
                "min": min(nums),
                "max": max(nums),
                "mean": round(statistics.fmean(nums), 4),
                "stddev": round(statistics.pstdev(nums), 4) if len(nums) > 1 else 0.0,
                "nulls": nulls,
            }
        elif col_type == "date" and non_null:
            stats[col_name] = {
                "min": min(non_null),
                "max": max(non_null),
                "distinct": len(set(non_null)),
                "nulls": nulls,
            }
        else:
            distinct_values = set(non_null)
            col_stats: dict = {"distinct": len(distinct_values), "nulls": nulls}
            if 0 < len(distinct_values) <= _LOW_CARDINALITY_THRESHOLD:
                col_stats["top"] = [
                    {"value": value, "count": count}
                    for value, count in Counter(non_null).most_common(_TOP_VALUES_MAX)
                ]
            stats[col_name] = col_stats
    return stats


@mcp.tool()
def query_data() -> list:
    """Query the seeded sales dataset.

    Returns a list of MCP content blocks: a JSON envelope (TextContent) and a
    ResourceLink to the full CSV via a short-lived presigned S3 URL.  Envelope:
    `{status, row_count, summary_stats, sample_rows, full_results, next_steps}`.
    """
    _ensure_scope("query_data")
    if not _RESULTS_BUCKET or not _RESULTS_OBJECT_KEY:
        raise ToolError("Results bucket not configured")

    header, rows, size_bytes = _read_seed_csv()
    presigned_url = _s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": _RESULTS_BUCKET, "Key": _RESULTS_OBJECT_KEY},
        ExpiresIn=_PRESIGNED_URL_TTL_SECONDS,
    )
    sample_rows = [dict(zip(header, row)) for row in rows[:_SAMPLE_ROW_COUNT]]
    ttl_minutes = max(1, _PRESIGNED_URL_TTL_SECONDS // 60)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=_PRESIGNED_URL_TTL_SECONDS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    size_kb = max(1, round(size_bytes / 1024))

    next_steps = (
        f"Returned {len(rows):,} rows (~{size_kb:,} KB). Summary statistics and the "
        f"first {len(sample_rows)} rows are shown inline — use these for quick "
        "analysis. For full-row analysis, tell the user: download the CSV from "
        f"`presigned_url` (expires in ~{ttl_minutes} minutes) and drag it into this "
        "chat; the analysis tool can then process every row."
    )

    envelope = {
        "status": "success",
        "row_count": len(rows),
        "summary_stats": _compute_summary_stats(header, rows),
        "sample_rows": sample_rows,
        "full_results": {
            "format": "csv",
            "size_bytes": size_bytes,
            "presigned_url": presigned_url,
            "presigned_url_expires_at": expires_at,
            "presigned_url_ttl_seconds": _PRESIGNED_URL_TTL_SECONDS,
            "inline": False,
        },
        "next_steps": next_steps,
        "_meta": {"tool_version": _TOOL_VERSION},
    }

    return [
        TextContent(type="text", text=json.dumps(envelope)),
        ResourceLink(
            type="resource_link",
            uri=presigned_url,
            name="sample_sales.csv",
            description=f"Full dataset ({len(rows):,} rows, ~{size_kb:,} KB)",
            mimeType="text/csv",
        ),
    ]


# -- tools/list filter ---------------------------------------------------------
class ScopeFilterMiddleware(Middleware):
    """Drop tools the JWT-holder isn't entitled to call from `tools/list`.

    Default-deny mirrors `_ensure_scope`: a tool is only visible if the JWT
    bears `tool:*` or the matching `tool:<name>` scope.  This middleware only
    affects `tools/list`; `tools/call` enforcement lives in the tool body via
    `_ensure_scope` so the security boundary is the tool itself.
    """

    async def on_list_tools(
        self,
        context: MiddlewareContext[ListToolsRequest],
        call_next: CallNext[ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        scopes = set(_scopes_from_token())
        if "tool:*" in scopes:
            return tools
        visible = [t for t in tools if _TOOL_SCOPES.get(t.name, "__none__") in scopes]
        _LOGGER.info(
            "tools/list filter: %d -> %d (scopes=%s, names=%s)",
            len(tools),
            len(visible),
            sorted(scopes),
            [t.name for t in visible],
        )
        return visible


mcp.add_middleware(ScopeFilterMiddleware())


# -- ASGI app ------------------------------------------------------------------
# Streamable-HTTP transport, stateless: each Lambda invocation handles a single
# request without relying on in-memory state from prior invocations.  Mounted
# at `/mcp`; FastMCP also auto-mounts the protected resource metadata route at
# `/.well-known/oauth-protected-resource/mcp` (RFC 9728 §3.1).
app = mcp.http_app(path=_MCP_PATH, transport="http", stateless_http=True)
