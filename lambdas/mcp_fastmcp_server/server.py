"""FastMCP server with Google OAuth (OAuth Proxy) — runs on ECS Fargate.

Architecture (see README): Claude Desktop → ALB:443 → this container.
ALB is TLS-only; FastMCP serves all OAuth endpoints itself:
  /.well-known/oauth-protected-resource
  /.well-known/oauth-authorization-server
  /register      (DCR shim — Google itself does not implement DCR)
  /authorize     (delegates to Google with `hd=<domain>` and PKCE)
  /token
  /auth/callback (upstream callback from Google)
  /mcp           (Streamable HTTP MCP transport, Bearer-token gated)

Domain restriction is enforced twice:
  1. `hd=<domain>` is passed in the upstream authorize URL (UX hint).
  2. Server-side, every tool call verifies the cached `hd` (or falls back to
     the email domain for consumer Gmail) against ALLOWED_WORKSPACE_DOMAINS.

Three tools are exposed: get_weather, get_time, query_data.  query_data
returns a presigned S3 URL plus inline summary stats — same envelope as the
Lambda-based stack, so the same Claude prompt patterns apply.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.dependencies import get_access_token
from key_value.aio.stores.dynamodb import DynamoDBStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

# Note: aioboto3 is pinned to <14 in pyproject.toml because 14.x/15.x
# break DynamoDBStore's client lifecycle ("cannot reuse already awaited
# coroutine").  Revisit pin when py-key-value-aio updates.
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger("mcp-fargate-google")

# --- environment -----------------------------------------------------------

_GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
_GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
_PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
_JWT_SIGNING_KEY = os.environ["JWT_SIGNING_KEY"]
_STORAGE_ENC_KEY = os.environ["STORAGE_ENC_KEY"]

_ALLOWED_DOMAINS = {
    d.strip().lower()
    for d in os.environ.get("ALLOWED_WORKSPACE_DOMAINS", "").split(",")
    if d.strip()
}

_RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "")
_RESULTS_OBJECT_KEY = os.environ.get("RESULTS_OBJECT_KEY", "sample_sales.csv")
_PRESIGNED_URL_TTL_SECONDS = int(os.environ.get("PRESIGNED_URL_TTL_SECONDS", "300"))

_SAMPLE_ROW_COUNT = 50
_TOP_VALUES_MAX = 5
_LOW_CARDINALITY_THRESHOLD = 30
_TOOL_VERSION = "1.0.0-fargate-google"

_s3 = boto3.client("s3")

# --- auth provider ---------------------------------------------------------

_client_storage = FernetEncryptionWrapper(
    key_value=DynamoDBStore(
        table_name=os.environ["DDB_TABLE_NAME"],
        region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
    ),
    fernet=Fernet(_STORAGE_ENC_KEY.encode() if isinstance(_STORAGE_ENC_KEY, str) else _STORAGE_ENC_KEY),
)

_primary_domain = next(iter(_ALLOWED_DOMAINS - {"gmail.com"}), "")

_extra_authorize_params: dict[str, str] = {"prompt": "select_account"}
if _primary_domain:
    # Hint Google's account picker to filter to this Workspace org.
    # NOT a security boundary — server-side hd check below is the gate.
    _extra_authorize_params["hd"] = _primary_domain

auth = GoogleProvider(
    client_id=_GOOGLE_CLIENT_ID,
    client_secret=_GOOGLE_CLIENT_SECRET,
    base_url=_PUBLIC_BASE_URL,
    required_scopes=[
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ],
    extra_authorize_params=_extra_authorize_params,
    jwt_signing_key=_JWT_SIGNING_KEY,
    client_storage=_client_storage,
)

mcp = FastMCP(name="Acme MCP (Fargate + Google)", auth=auth)


# --- domain guard ----------------------------------------------------------

def _enforce_domain_or_raise() -> dict:
    """Reject the caller unless their hd / email-domain is allow-listed.

    Returns the validated identity dict on success.  Raises PermissionError
    on failure — FastMCP turns this into a structured tool error.
    """
    token = get_access_token()
    if token is None:
        raise PermissionError("No access token in request")

    claims = getattr(token, "claims", {}) or {}
    email = (claims.get("email") or "").lower()
    hd = (claims.get("hd") or "").lower()
    email_domain = email.rpartition("@")[2]

    # Workspace accounts: hd is authoritative.
    # Consumer Gmail: hd is absent — fall back to email_domain.
    domain = hd or email_domain

    if not _ALLOWED_DOMAINS:
        # Fail-closed: if the env var is empty, deny everyone.
        raise PermissionError("Server misconfiguration: no allowed domains")

    if domain not in _ALLOWED_DOMAINS:
        _LOGGER.warning(
            "Rejecting user email=%s hd=%s domain=%s — not in allowlist", email, hd, domain
        )
        raise PermissionError(f"Domain {domain!r} is not allowed")

    if claims.get("email_verified") is False:
        raise PermissionError("Google email is not verified")

    return {"email": email, "hd": hd, "domain": domain}


# --- tools -----------------------------------------------------------------


@mcp.tool
async def get_weather(location: str) -> dict:
    """Get weather for a location."""
    _enforce_domain_or_raise()
    return {"weather": f"Weather in {location}: 72F, sunny"}


@mcp.tool
async def get_time(timezone_name: str = "UTC") -> dict:
    """Get current time for an IANA timezone (e.g. America/New_York)."""
    _enforce_domain_or_raise()
    try:
        now = datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return {"time": f"Current time in {timezone_name}: {now}"}


def _read_seed_csv() -> tuple[list[str], list[list[str]], int]:
    obj = _s3.get_object(Bucket=_RESULTS_BUCKET, Key=_RESULTS_OBJECT_KEY)
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
            distinct = set(non_null)
            col_stats: dict = {"distinct": len(distinct), "nulls": nulls}
            if 0 < len(distinct) <= _LOW_CARDINALITY_THRESHOLD:
                col_stats["top"] = [
                    {"value": v, "count": c}
                    for v, c in Counter(non_null).most_common(_TOP_VALUES_MAX)
                ]
            stats[col_name] = col_stats
    return stats


@mcp.tool
async def query_data() -> dict:
    """Query the sales sample dataset.

    Returns a JSON envelope with: status, row_count, summary_stats,
    sample_rows (first 50), full_results (presigned_url + ttl), next_steps.
    For full-row analysis, instruct the user to download the CSV and drop it
    back into the chat.
    """
    _enforce_domain_or_raise()

    if not _RESULTS_BUCKET:
        raise RuntimeError("RESULTS_BUCKET is not configured")

    header, rows, size_bytes = _read_seed_csv()
    url = _s3.generate_presigned_url(
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
        f"Returned {len(rows):,} rows (~{size_kb:,} KB).  Summary statistics "
        f"and the first {len(sample_rows)} rows are shown inline — use these "
        "for quick analysis.  For full-row analysis, tell the user: download "
        f"the CSV from `presigned_url` (expires in ~{ttl_minutes} minutes) "
        "and drag it into this chat; the analysis tool can then process every row."
    )
    return {
        "status": "success",
        "row_count": len(rows),
        "summary_stats": _compute_summary_stats(header, rows),
        "sample_rows": sample_rows,
        "full_results": {
            "format": "csv",
            "size_bytes": size_bytes,
            "presigned_url": url,
            "presigned_url_expires_at": expires_at,
            "presigned_url_ttl_seconds": _PRESIGNED_URL_TTL_SECONDS,
            "inline": False,
        },
        "next_steps": next_steps,
        "_meta": {"tool_version": _TOOL_VERSION},
    }


# --- HTTP app + health route ----------------------------------------------

app = mcp.http_app()

# CORS — MCP Inspector runs in the browser at http://localhost:6274 and makes
# cross-origin fetches to /mcp, /register, /token, /.well-known/*. claude.ai
# is server-to-server and doesn't need this, but Inspector and any browser
# client do. Expose Mcp-Session-Id so JS clients can read it.
from starlette.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id", "Mcp-Protocol-Version"],
)


# ALB target group health check hits this path.  Must NOT require auth.
async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# Starlette `Route` registration on the FastMCP-generated ASGI app.
from starlette.routing import Route  # noqa: E402  (after app construction)

app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))

if __name__ == "__main__":
    # Local-dev convenience; production uses uvicorn launched by Dockerfile CMD.
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
