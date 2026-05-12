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
import json
import logging
import os
import statistics
import time
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

# --- Athena / Glue config (consumed by query_data_catalog) -----------------
# No default database — callers fully-qualify `db.table` or pass the
# `database` tool arg, since LF-tag grants can span multiple Gold DBs.
_ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "")
_ATHENA_RESULTS_BUCKET = os.environ.get("ATHENA_RESULTS_BUCKET", "")
_ATHENA_QUERY_TIMEOUT_SECONDS = int(os.environ.get("ATHENA_QUERY_TIMEOUT_SECONDS", "600"))
_INLINE_MAX_BYTES = int(os.environ.get("ATHENA_INLINE_MAX_BYTES", "102400"))
_INLINE_MAX_ROWS = int(os.environ.get("ATHENA_INLINE_MAX_ROWS", "1000"))

_SAMPLE_ROW_COUNT = 50
_TOP_VALUES_MAX = 5
_LOW_CARDINALITY_THRESHOLD = 30
_TOOL_VERSION = "1.0.0-fargate-google"

_s3 = boto3.client("s3")
_athena = boto3.client("athena")

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


# --- query_data_catalog: SQL against Glue Data Catalog via Athena ---------
# Tag-based access: LakeFormation restricts the task role to tables tagged
# LakehouseLayer=Gold. Querying any other table returns an Athena
# AccessDeniedException surfaced as a structured error.

def _qdc_start_query(sql: str, database: str | None) -> str:
    kwargs: dict = {"QueryString": sql, "WorkGroup": _ATHENA_WORKGROUP}
    if database:
        kwargs["QueryExecutionContext"] = {"Database": database}
    return _athena.start_query_execution(**kwargs)["QueryExecutionId"]


def _qdc_wait_for_query(qid: str) -> dict:
    deadline = time.time() + _ATHENA_QUERY_TIMEOUT_SECONDS
    backoff = 0.5
    while True:
        execution = _athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = execution["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return execution
        if time.time() >= deadline:
            try:
                _athena.stop_query_execution(QueryExecutionId=qid)
            except Exception:
                pass
            raise TimeoutError(
                f"Athena query did not finish within {_ATHENA_QUERY_TIMEOUT_SECONDS}s"
            )
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 5.0)


def _qdc_parse_s3_uri(uri: str) -> tuple[str, str]:
    assert uri.startswith("s3://"), f"unexpected Athena output URI {uri!r}"
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def _qdc_read_result_csv(output_location: str) -> tuple[list[str], list[list[str]], int]:
    bucket, key = _qdc_parse_s3_uri(output_location)
    obj = _s3.get_object(Bucket=bucket, Key=key)
    size_bytes = int(obj.get("ContentLength", 0))
    text = obj["Body"].read().decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    rows = list(reader)
    return header, rows, size_bytes


def _qdc_error(
    error_type: str,
    message: str,
    *,
    qid: str | None = None,
    state: str | None = None,
    sql: str | None = None,
) -> dict:
    body: dict = {
        "status": "error",
        "error": {"type": error_type, "message": message},
        "_meta": {"tool_version": _TOOL_VERSION},
    }
    if qid:
        body["error"]["query_execution_id"] = qid
    if state:
        body["error"]["athena_state"] = state
    if sql:
        body["error"]["sql"] = sql
    return body


@mcp.tool
async def query_data_catalog(sql: str, database: str | None = None) -> dict:
    """Run a SQL query against the Glue Data Catalog via Athena.

    The query MUST be valid Athena SQL (Trino dialect, engine v3).
    Common dialect notes:
      - Date literal: `DATE '2024-01-01'` (no Postgres-style `::date`).
      - Cast: `CAST(x AS type)` only — no `::` casts.
      - String concat: `||` or `CONCAT(...)`.
      - Identifier quoting: double quotes (`"col name"`).
      - String literals: single quotes only (`'value'`).
      - Regex: `regexp_like(col, 'pattern')` (case-sensitive).
      - Date math: `date_add('day', 7, col)`, `date_diff('day', a, b)`.
      - JSON: `json_extract_scalar(col, '$.field')`.
      - Row limit: `LIMIT n` only — no `TOP n` / `FETCH FIRST`.

    Args:
        sql: The SQL query. Reference tables as `database.table` to query
            across multiple Gold-tagged databases in a single statement.
        database: Optional default database for unqualified table refs.
            Equivalent to a `USE <database>` prefix.

    Only resources tagged LakehouseLayer=Gold are accessible (enforced by
    LakeFormation). Querying other tables returns a structured error.
    Small results (<=100KB, <=1000 rows) are returned inline; larger
    results return a presigned S3 URL to the query output CSV.
    """
    _enforce_domain_or_raise()

    if not _ATHENA_WORKGROUP or not _ATHENA_RESULTS_BUCKET:
        return _qdc_error(
            "configuration_error",
            "Athena env vars not set (ATHENA_WORKGROUP / ATHENA_RESULTS_BUCKET)",
        )
    if not isinstance(sql, str) or not sql.strip():
        return _qdc_error("invalid_input", "sql must be a non-empty string", sql=sql)

    try:
        qid = _qdc_start_query(sql, database)
    except Exception as exc:
        _LOGGER.exception("Failed to start Athena query")
        return _qdc_error("athena_start_failed", str(exc), sql=sql)

    try:
        execution = _qdc_wait_for_query(qid)
    except TimeoutError as exc:
        return _qdc_error("timeout", str(exc), qid=qid, sql=sql)
    except Exception as exc:
        _LOGGER.exception("Failed while polling Athena query")
        return _qdc_error("athena_poll_failed", str(exc), qid=qid, sql=sql)

    status = execution["Status"]
    state = status["State"]
    if state != "SUCCEEDED":
        raw_reason = status.get("StateChangeReason") or f"Query ended in state {state}"
        # See Option B note: LF returns TABLE_NOT_FOUND both for missing
        # tables AND for permission-denied (hidden-deny). Reframe the
        # message so Claude knows the error is ambiguous, without leaking
        # whether the table exists.
        if "TABLE_NOT_FOUND" in raw_reason:
            err_type = "table_not_accessible"
            client_message = (
                f"{raw_reason}  Note: this error is returned either when the "
                "table does not exist OR when the caller's role lacks "
                "permission to read it. Through this tool, only tables tagged "
                "`LakehouseLayer=Gold` are queryable; tables in other layers "
                "appear as not-found regardless of whether they exist."
            )
        else:
            err_type = "athena_query_failed"
            client_message = raw_reason
        return _qdc_error(
            err_type,
            client_message,
            qid=qid,
            state=state,
            sql=sql,
        )

    output_location = execution["ResultConfiguration"]["OutputLocation"]
    try:
        header, rows, size_bytes = _qdc_read_result_csv(output_location)
    except Exception as exc:
        _LOGGER.exception("Failed to read Athena result CSV")
        return _qdc_error("result_read_failed", str(exc), qid=qid, sql=sql)

    row_count = len(rows)
    inline_candidate = [
        {col: (row[idx] if idx < len(row) else "") for idx, col in enumerate(header)}
        for row in rows
    ]
    inline_serialized_bytes = len(json.dumps(inline_candidate, default=str).encode("utf-8"))

    if row_count <= _INLINE_MAX_ROWS and inline_serialized_bytes <= _INLINE_MAX_BYTES:
        return {
            "status": "success",
            "inline": True,
            "query_execution_id": qid,
            "schema": header,
            "row_count": row_count,
            "rows": inline_candidate,
            "next_steps": (
                f"Returned {row_count:,} rows inline (~{inline_serialized_bytes:,} B). "
                "Use the rows directly to answer the user's question."
            ),
            "_meta": {"tool_version": _TOOL_VERSION},
        }

    bucket, key = _qdc_parse_s3_uri(output_location)
    presigned_url = _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=_PRESIGNED_URL_TTL_SECONDS,
    )
    ttl_minutes = max(1, _PRESIGNED_URL_TTL_SECONDS // 60)
    size_kb = max(1, round(size_bytes / 1024))
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=_PRESIGNED_URL_TTL_SECONDS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "status": "success",
        "inline": False,
        "query_execution_id": qid,
        "schema": header,
        "row_count": row_count,
        "full_results": {
            "format": "csv",
            "size_bytes": size_bytes,
            "presigned_url": presigned_url,
            "presigned_url_ttl_seconds": _PRESIGNED_URL_TTL_SECONDS,
            "presigned_url_expires_at": expires_at,
            "s3_uri": output_location,
        },
        "next_steps": (
            f"Returned {row_count:,} rows (~{size_kb:,} KB) — too large to inline. "
            f"Tell the user: download the CSV from `full_results.presigned_url` "
            f"(expires in ~{ttl_minutes} minutes) and drag it into this chat."
        ),
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
